"""Release gate for a diff: schema comparison, Pass / Warn / Fail rules, Markdown report.

Pure functions over the result dict built by the diff job (no I/O), so every
rule is unit-tested in isolation.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass, fields
from typing import Any

LEVELS = ("fail", "warn", "info")
VERDICT_LABEL = {"pass": "PASS", "warn": "WARN", "fail": "FAIL"}
# Columns that change on every build and say nothing about the buildings.
DEFAULT_IGNORE_COLS = ("build_id", "updated_at", "updated_by", "description", "winning_observation_id",
                       "config_version")
MAX_LISTED = 12


class GateError(ValueError):
    """Invalid threshold configuration (message is shown to the user)."""


@dataclass(frozen=True)
class Thresholds:
    """Release-gate limits. Percentages are of A unless stated otherwise."""

    fail_count_pct: float = 10.0  # |B − A| / A
    warn_count_pct: float = 2.0
    fail_removed_pct: float = 5.0  # removed / A
    warn_removed_pct: float = 1.0
    geom_area_pct: float = 20.0  # a matched building whose area moved more than this…
    geom_shift_m: float = 10.0  # …or whose centroid moved more than this is a "major" geometry change
    warn_geom_major_pct: float = 2.0  # major geometry changes / matched
    warn_real_drop_pts: float = 1.0  # % of buildings with a measured height, drop in points
    warn_null_increase_pts: float = 5.0  # null rate of a column, increase in points
    height_tol_m: float = 0.5  # |Δh| above this counts as a height change
    fail_layer_change: bool = True  # PMTiles layer renamed (breaks "source-layer" in app styles)


def parse_thresholds(raw: dict[str, Any] | None) -> Thresholds:
    """Validate thresholds sent by the UI; missing keys keep their defaults.

    Raises:
        GateError: On a non-numeric or negative value.
    """
    raw = raw or {}
    values: dict[str, Any] = {}
    for f in fields(Thresholds):
        if f.name not in raw or raw[f.name] in (None, ""):
            continue
        if f.type == "bool":
            values[f.name] = bool(raw[f.name])
            continue
        try:
            number = float(raw[f.name])
        except (TypeError, ValueError) as exc:
            raise GateError(f"Ngưỡng '{f.name}' phải là số.") from exc
        if not math.isfinite(number) or number < 0:
            raise GateError(f"Ngưỡng '{f.name}' phải là số ≥ 0.")
        values[f.name] = number
    t = Thresholds(**values)
    if t.warn_count_pct > t.fail_count_pct or t.warn_removed_pct > t.fail_removed_pct:
        raise GateError("Ngưỡng Warn phải ≤ ngưỡng Fail.")
    return t


# ---------------------------------------------------------------------- schema
def type_family(gdal_type: str, subtype: str | None = None) -> str:
    """GDAL field type -> int | real | bool | text | temporal | binary | list | other."""
    if subtype == "Boolean":
        return "bool"
    if gdal_type in ("Integer", "Integer64"):
        return "int"
    if gdal_type == "Real":
        return "real"
    if gdal_type in ("String",):
        return "text"
    if gdal_type in ("Date", "Time", "DateTime"):
        return "temporal"
    if gdal_type == "Binary":
        return "binary"
    if gdal_type.endswith("List"):
        return "list"
    return "other"


def type_change(a: dict[str, Any], b: dict[str, Any], tiles_involved: bool) -> tuple[str, str] | None:
    """Classify a field type change.

    Args:
        a, b: Field dicts with "type" and optional "subtype".
        tiles_involved: True when either side is PMTiles (MVT only has text / number / bool).

    Returns:
        None when unchanged, else (level, note) with level in fail / warn / info.
    """
    ta, tb = (a["type"], a.get("subtype")), (b["type"], b.get("subtype"))
    if ta == tb:
        return None
    fa, fb = type_family(*ta), type_family(*tb)
    pair = {fa, fb}
    if fa == fb:
        return "info", "cùng nhóm kiểu (đổi độ rộng / độ chính xác)"
    if pair == {"int", "real"}:
        if tiles_involved:
            return "info", "PMTiles lưu mọi số dạng số thực"
        return ("info", "số nguyên → số thực") if fa == "int" else ("warn", "số thực → số nguyên: mất phần thập phân")
    if tiles_involved and pair in ({"temporal", "text"}, {"bool", "int"}, {"bool", "real"}):
        return "info", "khác biệt do định dạng PMTiles (MVT không có kiểu ngày giờ)"
    return "fail", f"{fa} → {fb}"


def compare_schema(a: dict[str, Any], b: dict[str, Any], nonnull: dict[str, dict[str, int]],
                   required: list[str] | None) -> dict[str, Any]:
    """Compare the declared fields of A and B.

    Args:
        a, b: Side summaries with "fields" (list of {name, type, subtype}), "kind" and "buildings".
        nonnull: {"a": {field: non-null count}, "b": {...}} at building level.
        required: Fields B must keep; None means every field of A, an empty list means none.

    Returns:
        missing / added / type_changed / null_changes lists for the report and the gate.
    """
    fa = {f["name"]: f for f in a["fields"]}
    fb = {f["name"]: f for f in b["fields"]}
    na, nb = a.get("buildings") or 0, b.get("buildings") or 0
    req = set(fa) if required is None else set(required)
    tiles = "pmtiles" in (a.get("kind"), b.get("kind"))

    def pct(count: int | None, total: int) -> float | None:
        return None if count is None or not total else round(100.0 * count / total, 2)

    missing = [{"name": n, "type": f["type"], "a_filled_pct": pct(nonnull.get("a", {}).get(n), na),
                "required": n in req} for n, f in fa.items() if n not in fb]
    added = [{"name": n, "type": f["type"], "b_filled_pct": pct(nonnull.get("b", {}).get(n), nb)}
             for n, f in fb.items() if n not in fa]
    type_changed = []
    null_changes = []
    for name in (n for n in fa if n in fb):
        change = type_change(fa[name], fb[name], tiles)
        if change:
            type_changed.append({"name": name, "type_a": _type_label(fa[name]), "type_b": _type_label(fb[name]),
                                 "level": change[0], "note": change[1]})
        filled_a, filled_b = pct(nonnull.get("a", {}).get(name), na), pct(nonnull.get("b", {}).get(name), nb)
        if filled_a is not None and filled_b is not None:
            null_a, null_b = round(100 - filled_a, 2), round(100 - filled_b, 2)
            if abs(null_b - null_a) >= 0.01:
                null_changes.append({"name": name, "a_null_pct": null_a, "b_null_pct": null_b,
                                     "delta_pts": round(null_b - null_a, 2)})
    missing.sort(key=lambda m: (not m["required"], -(m["a_filled_pct"] or 0), m["name"]))
    null_changes.sort(key=lambda r: -r["delta_pts"])
    return {
        "fields_a": len(fa), "fields_b": len(fb), "common": len(set(fa) & set(fb)),
        "required": None if required is None else sorted(req & set(fa)),
        "missing": missing, "added": added, "type_changed": type_changed, "null_changes": null_changes,
    }


def _type_label(f: dict[str, Any]) -> str:
    return f"{f['type']}({f['subtype']})" if f.get("subtype") else f["type"]


# ---------------------------------------------------------------------- gate
def evaluate(result: dict[str, Any], t: Thresholds) -> dict[str, Any]:
    """Apply the release rules to a diff result.

    Returns:
        {"verdict": "pass" | "warn" | "fail", "findings": [{level, code, title, detail}], "counts": {...}}
    """
    findings: list[dict[str, str]] = []

    def add(level: str, code: str, title: str, detail: str = "") -> None:
        findings.append({"level": level, "code": code, "title": title, "detail": detail})

    sa, sb = result["sides"]["a"], result["sides"]["b"]
    schema, match, changes = result["schema"], result["match"], result["changes"]
    qa, qb = result["quality"]["a"], result["quality"]["b"]
    na, nb = sa.get("buildings") or 0, sb.get("buildings") or 0

    # --- overview / format
    if sa["kind"] != sb["kind"]:
        add("info", "cross_format", "A và B khác định dạng",
            f"A là {sa['kind']}, B là {sb['kind']}: kiểu dữ liệu và độ chính xác geometry (lượng tử hoá tile) "
            "có thể khác vì định dạng, không phải vì dữ liệu.")
    if sa["kind"] == sb["kind"] == "vector" and (sa.get("crs") or None) != (sb.get("crs") or None):
        add("fail", "crs_changed", "CRS khác A", f"A: {sa.get('crs') or 'không khai báo'} · B: {sb.get('crs') or 'không khai báo'}")
    new_families = sorted(set(sb.get("geometry_families", {})) - set(sa.get("geometry_families", {})))
    if new_families and na:
        add("fail", "geom_type_new", "B có kiểu geometry mà A không có",
            ", ".join(f"{f} ({_i(sb['geometry_families'][f])})" for f in new_families))
    if sa["kind"] == sb["kind"] == "pmtiles":
        if sa.get("layer") != sb.get("layer"):
            add("fail" if t.fail_layer_change else "warn", "layer_changed", "Tên layer PMTiles đổi",
                f"A: '{sa.get('layer')}' → B: '{sb.get('layer')}'. Style dùng \"source-layer\" cũ sẽ không hiện building.")
        if (sa.get("minzoom"), sa.get("maxzoom")) != (sb.get("minzoom"), sb.get("maxzoom")):
            add("warn", "zoom_changed", "Dải zoom PMTiles đổi",
                f"A: z{sa.get('minzoom')}–{sa.get('maxzoom')} → B: z{sb.get('minzoom')}–{sb.get('maxzoom')}")
    for side, s in (("A", sa), ("B", sb)):
        declared, read = s.get("tilestats_count"), s.get("buildings")
        if s["kind"] == "pmtiles" and declared and read is not None and declared != read:
            why = ("tippecanoe đã bỏ hoặc gộp polygon quá nhỏ" if read < declared
                   else "một số building cắt qua biên tile không ghép lại được, hoặc polygon nhỏ bị tippecanoe gộp thành ô vuông")
            add("info", f"tiles_count_{side.lower()}", f"{side}: số building đọc từ tile khác tilestats",
                f"tilestats {_i(declared)} · đọc được ở z{s.get('zoom')} {_i(read)} "
                f"({_signed(100.0 * (read - declared) / declared)}%): {why}")

    # --- schema
    req_missing = [m for m in schema["missing"] if m["required"]]
    opt_missing = [m for m in schema["missing"] if not m["required"]]
    if req_missing:
        add("fail", "schema_missing", f"B thiếu {len(req_missing)} field bắt buộc của A", _names(req_missing))
    if opt_missing:
        add("warn", "schema_missing_optional", f"B thiếu {len(opt_missing)} field (không bắt buộc) của A",
            _names(opt_missing))
    for level in ("fail", "warn", "info"):
        items = [c for c in schema["type_changed"] if c["level"] == level]
        if items:
            add(level, f"schema_type_{level}", f"{len(items)} field đổi kiểu",
                "; ".join(f"{c['name']}: {c['type_a']} → {c['type_b']} ({c['note']})" for c in items[:MAX_LISTED]))
    for side, s in (("A", sa), ("B", sb)):
        if s.get("skipped_fields"):
            add("info", f"skipped_{side.lower()}", f"{side}: bỏ qua {len(s['skipped_fields'])} cột trùng tên cột nội bộ",
                ", ".join(s["skipped_fields"][:MAX_LISTED]))
    if schema["added"]:
        add("info", "schema_added", f"B có {len(schema['added'])} field mới", _names(schema["added"]))
    worse = [c for c in schema["null_changes"] if c["delta_pts"] > t.warn_null_increase_pts]
    if worse:
        add("warn", "null_increase", f"{len(worse)} cột có tỉ lệ null tăng hơn {_n(t.warn_null_increase_pts)} điểm %",
            "; ".join(f"{c['name']}: {_n(c['a_null_pct'])}% → {_n(c['b_null_pct'])}%" for c in worse[:MAX_LISTED]))

    # --- identity (IDs only gate the release when they are what A and B are matched on)
    key_mode = match["mode"] == "key"
    if match.get("empty_keys_b"):
        add("fail" if key_mode else "info", "empty_keys_b", "B có building không có ID",
            f"{_i(match['empty_keys_b'])} building")
    if match.get("dup_keys_b"):
        add("fail" if key_mode else "info", "dup_keys_b", "B có ID trùng",
            f"{_i(match['dup_keys_b'])} ID xuất hiện nhiều lần ({_i(match['dup_rows_b'])} building)")
    if key_mode:
        for code, label in (("empty_keys_a", "A có building không có ID"), ("dup_keys_a", "A có ID trùng")):
            if match.get(code):
                add("info", code, label, f"{_i(match[code])}")
    bad_geom = (sb.get("null_geometry") or 0) + (sb.get("empty_geometry") or 0)
    if bad_geom:
        add("fail", "null_geometry_b", "B có geometry NULL / rỗng", f"{_i(bad_geom)} building")
    if (sb.get("invalid_geometry") or 0) > (sa.get("invalid_geometry") or 0):
        add("warn", "invalid_geometry_up", "Số geometry không hợp lệ tăng",
            f"A {_i(sa.get('invalid_geometry') or 0)} → B {_i(sb.get('invalid_geometry') or 0)}")

    # --- counts
    if match["mode"] == "location":
        add("info", "location_mode", "Khớp theo vị trí (A và B không có ID chung)",
            "Building được ghép khi tâm gần nhau và diện tích tương đương; số thêm / xoá / đổi là ước lượng.")
    if na:
        delta_pct = 100.0 * (nb - na) / na
        level = "fail" if abs(delta_pct) > t.fail_count_pct else "warn" if abs(delta_pct) > t.warn_count_pct else None
        if level:
            limit = t.fail_count_pct if level == "fail" else t.warn_count_pct
            add(level, "count_change", f"Số building lệch {_signed(delta_pct)}%",
                f"A {_i(na)} → B {_i(nb)} (ngưỡng ±{_n(limit)}%)")
        removed_pct = 100.0 * match["removed"] / na
        level = "fail" if removed_pct > t.fail_removed_pct else "warn" if removed_pct > t.warn_removed_pct else None
        if level:
            limit = t.fail_removed_pct if level == "fail" else t.warn_removed_pct
            add(level, "removed", f"{_n(removed_pct)}% building của A bị xoá",
                f"{_i(match['removed'])} building (ngưỡng {_n(limit)}%)")
    reid = [x for x in (match.get("reid_same_location"), match.get("reid_same_source")) if x]
    if reid:
        parts = []
        if match.get("reid_same_location"):
            parts.append(f"{_i(match['reid_same_location'])} cặp xoá + thêm nằm cùng vị trí")
        if match.get("reid_same_source"):
            parts.append(f"{_i(match['reid_same_source'])} building bị xoá có {match['reid_source_col']} xuất hiện lại ở B")
        add("info", "reid", "Có building đổi ID", "; ".join(parts) + ". ID có thể không ổn định giữa 2 bản build.")

    # --- changes
    matched = match["matched"]
    if matched:
        major = changes["geometry"].get("major", 0)
        major_pct = 100.0 * major / matched
        if major_pct > t.warn_geom_major_pct:
            add("warn", "geom_major", f"{_n(major_pct)}% building khớp đổi geometry đáng kể",
                f"{_i(major)} building có diện tích lệch > {_n(t.geom_area_pct)}% hoặc tâm dịch > {_n(t.geom_shift_m)} m "
                f"(ngưỡng {_n(t.warn_geom_major_pct)}%)")

    # --- quality
    # Unknown on either side (no provenance / height column) says nothing about a drop.
    known = qa.get("real_pct") is not None and qb.get("real_pct") is not None
    drop = (qa["real_pct"] - qb["real_pct"]) if known else 0.0
    if drop > t.warn_real_drop_pts:
        add("warn", "real_height_drop", f"% building có chiều cao thật giảm {_n(drop)} điểm",
            f"A {_n(qa.get('real_pct'))}% → B {_n(qb.get('real_pct'))}%")
    if (qb.get("outliers") or 0) > (qa.get("outliers") or 0):
        add("warn", "outliers_up", "Số outlier chiều cao tăng", f"A {_i(qa.get('outliers') or 0)} → B {_i(qb.get('outliers') or 0)}")
    for key, label in (("orphan_parts", "Khối con mồ côi (parent không tồn tại) tăng"),
                       ("dangling_superseded", "superseded_by trỏ tới ID không tồn tại tăng")):
        if qb.get(key) is not None and (qb.get(key) or 0) > (qa.get(key) or 0):
            add("warn", f"{key}_up", label, f"A {qa.get(key) if qa.get(key) is not None else '—'} → B {_i(qb[key])}")

    order = {level: i for i, level in enumerate(LEVELS)}
    findings.sort(key=lambda f: order[f["level"]])
    counts = {level: sum(1 for f in findings if f["level"] == level) for level in LEVELS}
    verdict = "fail" if counts["fail"] else "warn" if counts["warn"] else "pass"
    return {"verdict": verdict, "findings": findings, "counts": counts, "thresholds": asdict(t)}


def _names(items: list[dict[str, Any]]) -> str:
    shown = ", ".join(i["name"] for i in items[:MAX_LISTED])
    return shown + (f" … (+{len(items) - MAX_LISTED})" if len(items) > MAX_LISTED else "")


def _n(value: float | None, digits: int = 2) -> str:
    """Vietnamese number format: 1.234,5 (trailing zeros dropped)."""
    if value is None:
        return "—"
    text = f"{value:,.{digits}f}"
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return text.replace(",", "\0").replace(".", ",").replace("\0", ".") or "0"


def _i(value: int | float | None) -> str:
    """Vietnamese thousands separator: 1.197.099."""
    return "—" if value is None else f"{int(value):,}".replace(",", ".")


def _signed(value: float) -> str:
    return ("+" if value > 0 else "") + _n(value)


# ---------------------------------------------------------------------- markdown
def render_markdown(result: dict[str, Any]) -> str:
    """Render diff_report.md (for a ticket / chat)."""
    gate = result["gate"]
    sa, sb = result["sides"]["a"], result["sides"]["b"]
    match, changes, schema = result["match"], result["changes"], result["schema"]
    icon = {"pass": "✅", "warn": "⚠️", "fail": "⛔"}[gate["verdict"]]
    lines = [
        f"# So sánh trước release — {VERDICT_LABEL[gate['verdict']]} {icon}", "",
        f"- **A (production):** `{sa['name']}` — {sa['kind']}, {_i((sa.get('buildings') or 0))} building",
        f"- **B (release):** `{sb['name']}` — {sb['kind']}, {_i((sb.get('buildings') or 0))} building",
        f"- Khớp theo: {'ID `' + str(match.get('key_a')) + '`' if match['mode'] == 'key' else 'vị trí (không có ID chung)'}",
        f"- Thời gian: {result.get('created', '')}", "",
        "## Release gate", "",
        "| Mức | Kiểm tra | Chi tiết |", "|---|---|---|",
    ]
    for f in gate["findings"]:
        lines.append(f"| {f['level'].upper()} | {f['title']} | {_md(f['detail'])} |")
    if not gate["findings"]:
        lines.append("| PASS | Không vi phạm ngưỡng nào | |")

    lines += ["", "## Tổng quan", "", "| | A | B |", "|---|---:|---:|"]
    for label, key in (("Building", "buildings"), ("Mảnh tile (PMTiles)", "pieces"), ("Geometry NULL", "null_geometry"),
                       ("Geometry rỗng", "empty_geometry"), ("Geometry không hợp lệ", "invalid_geometry")):
        lines.append(f"| {label} | {_cell(sa.get(key))} | {_cell(sb.get(key))} |")
    lines.append(f"| CRS | {sa.get('crs') or '—'} | {sb.get('crs') or '—'} |")
    lines.append(f"| Dung lượng | {_size(sa.get('size'))} | {_size(sb.get('size'))} |")
    if "pmtiles" in (sa["kind"], sb["kind"]):
        lines.append(f"| Layer / zoom | {_layer_zoom(sa)} | {_layer_zoom(sb)} |")
    for col in sorted(set(sa.get("values", {})) | set(sb.get("values", {}))):
        top_a = ", ".join(str(v["value"]) for v in sa.get("values", {}).get(col, {}).get("top", [])[:3])
        top_b = ", ".join(str(v["value"]) for v in sb.get("values", {}).get(col, {}).get("top", [])[:3])
        lines.append(f"| {col} | {_md(top_a) or '—'} | {_md(top_b) or '—'} |")

    lines += ["", "## Khớp building", "",
              f"- Khớp: **{_i(match['matched'])}** (không đổi {_i(match['unchanged'])} · có thay đổi {_i(match['changed'])})",
              f"- Thêm mới (chỉ có ở B): **{_i(match['added'])}**",
              f"- Bị xoá (chỉ có ở A): **{_i(match['removed'])}**"]
    for s in ("a", "b") if match["mode"] == "key" else ():
        if match.get(f"dup_keys_{s}") or match.get(f"empty_keys_{s}"):
            lines.append(f"- {s.upper()}: ID trùng {_i(match.get(f'dup_keys_{s}') or 0)} · ID rỗng {_i(match.get(f'empty_keys_{s}') or 0)}")

    g = changes["geometry"]
    h = changes["height"]
    lines += ["", "## Thay đổi ở building khớp", "",
              f"- Geometry: giống hệt {_i(g['identical'])} · khác nhỏ {_i(g['minor'])} · khác vừa {_i(g['moderate'])} · "
              f"khác đáng kể {_i(g['major'])}",
              f"- Chiều cao: đổi {_i(h['changed'])} (cao lên {_i(h['taller'])} · thấp xuống {_i(h['lower'])}) · "
              f"mặc định → có số đo {_i(h['default_to_real'])} · có số đo → mặc định {_i(h['real_to_default'])}",
              "", "| Cột | Số building đổi | % | Bỏ qua |", "|---|---:|---:|---|"]
    for c in changes["columns"]:
        if c["changed"]:
            lines.append(f"| {c['column']} | {_i(c['changed'])} | {_n(c['pct'])} | {'✓' if c['ignored'] else ''} |")

    lines += ["", "## Schema", "", f"- Field: A {schema['fields_a']} · B {schema['fields_b']} · chung {schema['common']}"]
    if schema["missing"]:
        lines.append(f"- B thiếu: {', '.join(('**' + m['name'] + '**') if m['required'] else m['name'] for m in schema['missing'][:60])}"
                     + (" …" if len(schema["missing"]) > 60 else ""))
    if schema["added"]:
        lines.append(f"- B thêm: {', '.join(m['name'] for m in schema['added'][:60])}")
    for c in schema["type_changed"]:
        lines.append(f"- Đổi kiểu `{c['name']}`: {c['type_a']} → {c['type_b']} ({c['level']}: {c['note']})")

    qa, qb = result["quality"]["a"], result["quality"]["b"]
    lines += ["", "## Chất lượng", "", "| | A | B |", "|---|---:|---:|",
              f"| Có chiều cao thật | {_real(qa)} | {_real(qb)} |",
              f"| Outlier chiều cao | {_cell(qa['outliers'])} | {_cell(qb['outliers'])} |",
              f"| Khối con mồ côi | {_cell(qa['orphan_parts'])} | {_cell(qb['orphan_parts'])} |",
              f"| superseded_by treo | {_cell(qa['dangling_superseded'])} | {_cell(qb['dangling_superseded'])} |"]

    grid = result["grid"]
    if grid["top"]:
        lines += ["", f"## Khu vực thay đổi nhiều nhất (ô {grid['cell_deg']}°)", "",
                  "| Tâm ô (lat, lon) | A | B | Thêm | Xoá | Đổi |", "|---|---:|---:|---:|---:|---:|"]
        for c in grid["top"]:
            lines.append(f"| {c['center'][1]:.3f}, {c['center'][0]:.3f} | {_i(c['n_a'])} | {_i(c['n_b'])} | "
                         f"{_i(c['added'])} | {_i(c['removed'])} | {_i(c['changed'])} |")
    return "\n".join(lines) + "\n"


def _real(quality: dict[str, Any]) -> str:
    if quality.get("real_pct") is None:
        return "không xác định"
    return f"{_cell(quality['real_height'])} ({_n(quality['real_pct'])}%)"


def _size(num_bytes: int | None) -> str:
    if num_bytes is None:
        return "—"
    value, unit = float(num_bytes), "B"
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1000 or unit == "GB":
            break
        value /= 1000
    return f"{_n(value, 1)} {unit}"


def _layer_zoom(side: dict[str, Any]) -> str:
    if side["kind"] != "pmtiles":
        return "—"
    return f"{side.get('layer') or '—'} z{side.get('minzoom')}–{side.get('maxzoom')}"


def _cell(value: Any) -> str:
    if value is None:
        return "—"
    return f"{_i(value)}" if isinstance(value, int) else str(value)


def _md(text: Any) -> str:
    return str(text or "").replace("|", "\\|").replace("\n", " ")
