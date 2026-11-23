"""QC lists as CSV: a canonical build's Parquet and review GeoPackages, for QC people who open
them in Excel or Google Sheets rather than QGIS.

    <build>/canonical_building_<area>.parquet ─┐
    <build>/review_<source>.gpkg ──────────────┴─ DuckDB spatial ──► <build>/qc/*.csv

Each row keeps its attributes; the footprint becomes a point on its surface (lat, lon), a Google
Maps link and the H3 r8 cell it falls in (the cells the /preview map draws to split QC between
people). Files are UTF-8 with a BOM, so Excel shows Vietnamese names, and rows are sorted by cell.
A list longer than Excel's sheet limit is split into parts, a cell never straddling two parts.
"""

from __future__ import annotations

import shutil
from collections.abc import Callable
from pathlib import Path

from .sqlbuild import sql_str

QC_DIR = "qc"
H3_RES = 8
# Excel stops at 1,048,576 rows; parts are cut between cells, so leave room for the last cell.
PART_ROWS = 1_000_000
BOM = b"\xef\xbb\xbf"
MAP_URL = "https://www.google.com/maps?q="


def export_build(folder: Path, files: dict[str, str], log: Callable[[str], None] = lambda _: None) -> list[Path]:
    """Write the QC CSVs of one build (files: the "files" of its build.json) to <folder>/qc/."""
    import duckdb

    from .osmbase import load_extension

    out_dir = folder / QC_DIR
    out_dir.mkdir(exist_ok=True)
    for old in out_dir.glob("*.csv"):
        old.unlink()
    written: list[Path] = []
    with duckdb.connect() as con:
        load_extension(con, "spatial")
        parquet = folder / files["parquet"]
        written += _export(con, f"read_parquet({sql_str(str(parquet))})", "geometry", out_dir / f"qc_{parquet.stem}", log)
        for key, name in sorted(files.items()):
            if key.startswith("review_") and (folder / name).is_file():
                written += _export(con, f"ST_Read({sql_str(str(folder / name))})", "geom",
                                   out_dir / f"qc_{Path(name).stem}", log, drop=("fid",))
    return written


def _h3_cells(con, path: Path) -> None:
    """qc_h3(qc_i, h3_r8) from qc_pt's points, with the h3 library (a DuckDB UDF would need numpy)."""
    import csv

    import h3

    cur = con.execute("SELECT qc_i, lat, lon FROM qc_pt WHERE lat IS NOT NULL")
    with path.open("w", newline="") as f:
        out = csv.writer(f)
        out.writerow(("qc_i", "h3_r8"))
        while rows := cur.fetchmany(100_000):
            out.writerows((i, h3.latlng_to_cell(lat, lon, H3_RES)) for i, lat, lon in rows)
    con.execute(f"""CREATE OR REPLACE TEMP TABLE qc_h3 AS
        SELECT * FROM read_csv({sql_str(str(path))}, header = true, columns = {{'qc_i': 'BIGINT', 'h3_r8': 'VARCHAR'}})""")
    path.unlink()


def _export(con, source: str, geom: str, stem: Path, log: Callable[[str], None],
            drop: tuple[str, ...] = ()) -> list[Path]:
    """One source -> stem.csv, or stem_part1.csv, stem_part2.csv… when it is too long for Excel."""
    exclude = ", ".join(f'"{c}"' for c in (geom, *drop))
    con.execute(f"""CREATE OR REPLACE TEMP TABLE qc_pt AS
        SELECT row_number() OVER () AS qc_i, round(ST_Y(p), 7) AS lat, round(ST_X(p), 7) AS lon, * EXCLUDE (p)
        FROM (SELECT *, ST_PointOnSurface("{geom}") AS p FROM {source})""")
    _h3_cells(con, stem.with_suffix(".h3.tmp"))
    con.execute(f"""CREATE OR REPLACE TEMP TABLE qc_rows AS
        SELECT h.h3_r8, t.lat, t.lon,
               CASE WHEN t.lat IS NOT NULL THEN {sql_str(MAP_URL)} || t.lat || ',' || t.lon END AS map_link,
               t.* EXCLUDE (lat, lon, {exclude})
        FROM qc_pt AS t LEFT JOIN qc_h3 AS h USING (qc_i)""")
    # Whole cells go to a part: the part of a cell is where its first row falls.
    con.execute(f"""CREATE OR REPLACE TEMP TABLE qc_parts AS
        SELECT h3_r8, (sum(n) OVER (ORDER BY h3_r8 NULLS LAST ROWS UNBOUNDED PRECEDING) - n) // {PART_ROWS} AS part
        FROM (SELECT h3_r8, count(*) AS n FROM qc_rows GROUP BY h3_r8)""")
    parts = con.execute("SELECT count(DISTINCT part) FROM qc_parts").fetchone()[0] or 1
    written = []
    for part in range(parts):
        path = stem.with_name(stem.name + (f"_part{part + 1}" if parts > 1 else "") + ".csv")
        tmp = path.with_suffix(".tmp")
        con.execute(f"""COPY (SELECT r.* EXCLUDE (qc_i) FROM qc_rows AS r
                              JOIN qc_parts AS p ON p.h3_r8 IS NOT DISTINCT FROM r.h3_r8
                              WHERE p.part = {part} ORDER BY r.h3_r8 NULLS LAST, r.qc_i)
                        TO {sql_str(str(tmp))} (FORMAT CSV, HEADER)""")
        with path.open("wb") as dst, tmp.open("rb") as src:
            dst.write(BOM)
            shutil.copyfileobj(src, dst, 1 << 20)
        tmp.unlink()
        written.append(path)
    n = con.execute("SELECT count(*) FROM qc_rows").fetchone()[0]
    log(f"CSV QC: {n:,} dòng → " + ", ".join(p.name for p in written))
    con.execute("DROP TABLE qc_pt; DROP TABLE qc_h3; DROP TABLE qc_rows; DROP TABLE qc_parts")
    return written
