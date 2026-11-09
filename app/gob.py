"""Google Open Buildings v3: download the footprints of an area as a snapshot.

    tiles.geojson (Google) ─ cells of level 6 that cover the area (app.s2cell)
      └─ <token>_buildings.csv.gz (storage.googleapis.com, one file per S2 cell, no header)
           kept in sources/gob/raw/ (resumed when interrupted, reused by later snapshots)
           └─ DuckDB read_csv, rows inside the area's box ─► sources/gob/<snapshot>/gob.parquet + source.json

The CSV columns are latitude, longitude, area_in_meters, confidence, geometry (WKT polygon in
EPSG:4326), full_plus_code. Every row has confidence >= 0.65; the conflation keeps its own minimum
(osm-first-v1: 0.75). A snapshot is immutable: a later download makes a new folder.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import __version__, net, s2cell
from .sqlbuild import sql_str

TILE_URL = "https://storage.googleapis.com/open-buildings-data/v3/polygons_s2_level_6_gzip_no_header/{token}_buildings.csv.gz"
S2_LEVEL = 6
DATASET = "google_open_buildings"
DATASET_VERSION = "v3"
LICENSE = "CC BY-4.0 (Google Open Buildings) / ODbL"
ATTRIBUTION = "Google Open Buildings v3 (CC BY 4.0 / ODbL)"
CSV_COLUMNS = {"latitude": "DOUBLE", "longitude": "DOUBLE", "area_in_meters": "DOUBLE", "confidence": "DOUBLE",
               "geometry": "VARCHAR", "full_plus_code": "VARCHAR"}
CHUNK = 1 << 20
USER_AGENT = f"BuildingDataStudio/{__version__}"


class GobError(RuntimeError):
    """Google Open Buildings could not be fetched (message is shown to the user)."""


def tile_tokens(bbox: tuple[float, float, float, float]) -> list[str]:
    return s2cell.covering(bbox, S2_LEVEL)


def tile_url(token: str) -> str:
    return TILE_URL.format(token=token)


def _remote_size(url: str) -> int | None:
    req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=60, context=net.ssl_context()) as res:  # noqa: S310 — fixed https host
            return int(res.headers.get("Content-Length") or 0) or None
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return 0  # no buildings in this cell (sea)
        raise GobError(f"Google Open Buildings: HTTP {exc.code} cho {url}") from exc
    except OSError as exc:
        raise GobError(f"Không kết nối được Google Open Buildings: {exc}") from exc


def download(url: str, dest: Path, *, on_bytes: Callable[[int, int], None] | None = None,
             cancelled: Callable[[], bool] = lambda: False, attempts: int = 5) -> int:
    """Fetch url into dest, resuming a partial file (dest.part) with a Range request.

    A complete file (same size as the server's) is reused as is. Returns its size.
    """
    total = _remote_size(url)
    if total == 0:
        return 0
    if dest.exists() and total and dest.stat().st_size == total:
        return total
    part = dest.with_name(dest.name + ".part")
    dest.parent.mkdir(parents=True, exist_ok=True)
    last: Exception | None = None
    for attempt in range(attempts):
        have = part.stat().st_size if part.exists() else 0
        if total and have >= total:
            break
        headers = {"User-Agent": USER_AGENT}
        if have:
            headers["Range"] = f"bytes={have}-"
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=120, context=net.ssl_context()) as res:  # noqa: S310
                if have and res.status != 206:  # the server ignored the range: start over
                    have = 0
                    part.unlink(missing_ok=True)
                with part.open("ab") as fh:
                    while True:
                        if cancelled():
                            raise InterruptedError("Đã huỷ")
                        block = res.read(CHUNK)
                        if not block:
                            break
                        fh.write(block)
                        have += len(block)
                        if on_bytes:
                            on_bytes(have, total or have)
            break
        except (OSError, urllib.error.URLError) as exc:
            if isinstance(exc, InterruptedError):
                raise
            last = exc
            time.sleep(min(5 * 2 ** attempt, 60))
    else:
        raise GobError(f"Không tải được {url} sau {attempts} lần thử: {last}")
    size = part.stat().st_size
    if total and size != total:
        raise GobError(f"Tải {url} chưa đủ ({size:,}/{total:,} byte); bấm build lại để tải tiếp.")
    os.replace(part, dest)
    return size


def snapshot_sql(raw_files: list[Path], bbox: tuple[float, float, float, float], out: Path) -> str:
    """COPY the rows of the raw CSV files whose point lies in bbox into a GeoParquet."""
    w, s, e, n = (repr(float(v)) for v in bbox)
    files = "[" + ", ".join(sql_str(str(p)) for p in raw_files) + "]"
    cols = "{" + ", ".join(f"{sql_str(k)}: {sql_str(v)}" for k, v in CSV_COLUMNS.items()) + "}"
    return (
        "COPY (SELECT full_plus_code AS plus_code, confidence, area_in_meters, latitude, longitude, "
        "ST_GeomFromText(geometry) AS geometry "
        f"FROM read_csv({files}, header = false, columns = {cols}, compression = 'gzip', quote = '\"', "
        "parallel = false) "
        f"WHERE longitude BETWEEN {w} AND {e} AND latitude BETWEEN {s} AND {n} "
        f"ORDER BY plus_code) TO {sql_str(str(out))} (FORMAT PARQUET, COMPRESSION ZSTD)"
    )


def covers(box: list[float] | tuple[float, ...], inner: tuple[float, float, float, float]) -> bool:
    return box[0] <= inner[0] and box[1] <= inner[1] and box[2] >= inner[2] and box[3] >= inner[3]


def find_snapshot(root: Path, bbox: tuple[float, float, float, float]) -> dict[str, Any] | None:
    """The newest GOB snapshot whose box holds bbox."""
    found = []
    for meta in sorted(root.glob("*/source.json")):
        try:
            data = json.loads(meta.read_text("utf-8"))
        except (OSError, ValueError):
            continue
        if data.get("source_id") == DATASET and covers(data.get("bbox") or [0, 0, 0, 0], bbox) \
                and (meta.parent / data.get("file", "")).is_file():
            found.append({**data, "path": str(meta.parent / data["file"]), "folder": str(meta.parent)})
    found.sort(key=lambda d: d.get("downloaded_at") or "")
    return found[-1] if found else None


def make_snapshot(con: Any, root: Path, bbox: tuple[float, float, float, float], *,
                  log: Callable[[str], None] = print, on_progress: Callable[[float], None] | None = None,
                  cancelled: Callable[[], bool] = lambda: False) -> dict[str, Any]:
    """Download the GOB tiles over bbox (raw files are kept for reuse) and write a snapshot."""
    tokens = tile_tokens(bbox)
    log(f"Google Open Buildings v3: {len(tokens)} ô S2 cấp {S2_LEVEL} phủ khung {', '.join(f'{v:.3f}' for v in bbox)}: "
        + ", ".join(tokens))
    raw_dir = root / "raw"
    files: list[Path] = []
    for i, token in enumerate(tokens):
        url = tile_url(token)
        dest = raw_dir / f"{token}_buildings.csv.gz"
        started, shown = time.time(), [0.0]

        def on_bytes(have: int, total: int, i: int = i) -> None:
            if on_progress:
                on_progress(80.0 * (i + have / total) / len(tokens))
            now = time.time()
            if now - shown[0] > 15:
                shown[0] = now
                log(f"  {token}: {have / 1e6:,.0f}/{total / 1e6:,.0f} MB")

        reused = dest.exists()
        size = download(url, dest, on_bytes=on_bytes, cancelled=cancelled)
        if size == 0:
            log(f"  {token}: không có file (ô không có building)")
            continue
        log(f"  {token}: {size / 1e6:,.0f} MB " + ("(đã có, dùng lại)" if reused and time.time() - started < 5
                                                  else f"tải trong {time.time() - started:.0f}s"))
        files.append(dest)
    if not files:
        raise GobError("Không có file Google Open Buildings nào cho khu vực này.")
    stamp = time.strftime("%Y%m%d-%H%M%S")
    snap_id = f"gob-{DATASET_VERSION}-{stamp}"
    folder = root / snap_id
    folder.mkdir(parents=True, exist_ok=True)
    out = folder / "gob.parquet"
    log("Lọc building trong khung khu vực (DuckDB đọc CSV nén, vài phút với ô lớn)…")
    t = time.time()
    con.execute(snapshot_sql(files, bbox, out))
    rows = con.execute(f"SELECT count(*) FROM read_parquet({sql_str(str(out))})").fetchone()[0]
    data = {
        "source_id": DATASET, "dataset_version": DATASET_VERSION, "snapshot_id": snap_id,
        "role": "external_observation", "license": LICENSE, "attribution": ATTRIBUTION,
        "s2_level": S2_LEVEL, "tiles": [{"token": p.name.split("_")[0], "url": tile_url(p.name.split("_")[0]),
                                         "bytes": p.stat().st_size} for p in files],
        "bbox": [round(v, 6) for v in bbox], "rows": rows, "file": out.name,
        "downloaded_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "lineage": {"upstream_sources": [], "independent_observation": True},
    }
    (folder / "source.json").write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"Snapshot {snap_id}: {rows:,} building trong khung ({time.time() - t:.0f}s)")
    if on_progress:
        on_progress(100.0)
    return {**data, "path": str(out), "folder": str(folder)}
