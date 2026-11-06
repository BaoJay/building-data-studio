"""Landmark library: the GLB files of each landmark and the versions in use, kept on disk.

    landmarks/                    (BDS_LANDMARK_DIR)
      <id>/landmark.json          building, footprint, versions, placements, history
      <id>/v1.glb, v2.glb, …      every upload, never overwritten
      <id>/v2_z16.glb, …          more files of a version, one per zoom range (LOD)
      _trash/<id>__<time>/        deleted landmarks (moved, not erased)

A landmark replaces the extrusion of one building_id. Its footprint is read once, when it is
registered, from the PMTiles it was clicked on (landmark.lookup), so the library opens without
that file. `link` is a free id to find the building elsewhere (MapOps canonical id, OSM id…).

Each version has its own placement, since a new export may move the model's origin: heading
(degrees clockwise seen from above, as in the 3D preview), scale, the model point that goes on
the ground (GLB origin or bottom centre of its box) and the lon / lat it goes to. A new version
starts from the placement of the version in use, unless its GLB says where it stands
(asset.extras.anchor, written by the landmark pipeline): then heading 0 at that point, since such
a model is already turned to true north. One version at a time is in use; every switch is kept in
the history, so the versions that were used, and when, can be read back.

Levels of detail. A GLB may say which zooms it is for (asset.extras.zoom_range [min, max | null],
min <= zoom < max). A version can then hold several files of the same model, one per range (the
pipeline exports z15 / z16 / z17): the first upload is the version's own file (`file`, the most
detailed: shadows, box, download), the others are added to it (`lods`). They share its placement.
The 3D preview and the app draw the file whose range holds the zoom, none outside every range.
A file uploaded into one of the z15 / z16 / z17 slots takes the slot's range (LOD_SLOTS) instead of
the one its GLB says, so a GLB without asset.extras.zoom_range can be part of a set too.
"""

from __future__ import annotations

import getpass
import hashlib
import json
import math
import os
import re
import shutil
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from . import glb

APP_DIR = Path(__file__).resolve().parent
DEFAULT_DIR = APP_DIR.parent / "landmarks"
MANIFEST = "landmark.json"
TRASH = "_trash"
SCHEMA = 1
ANCHORS = ("origin", "bbox")
MAX_SCALE = 1000.0
MAX_TEXT = 500
_ID_RE = re.compile(r"[^A-Za-z0-9._-]+")
RESERVED_IDS = {"manifest.json", "render"}  # taken by an API route
# Upload slots per level of detail: lod_zoom -> zoom range [min, max | None) the file is drawn at.
LOD_SLOTS = {17: [17, None], 16: [15, 17], 15: [0, 15]}


class LibraryError(ValueError):
    pass


class NotFound(LibraryError):
    pass


def default_dir() -> Path:
    return Path(os.environ.get("BDS_LANDMARK_DIR") or DEFAULT_DIR).expanduser()


def now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def user() -> str:
    try:
        return getpass.getuser()
    except (OSError, KeyError):  # no login name (some containers)
        return ""


def _text(value: Any, label: str) -> str:
    text = str(value or "").strip()
    if len(text) > MAX_TEXT:
        raise LibraryError(f"{label} dài quá {MAX_TEXT} ký tự.")
    return text


def check_placement(raw: dict[str, Any]) -> dict[str, Any]:
    """Validated placement: heading in [0, 360), scale > 0, anchor point, lon / lat."""
    def num(key: str, label: str) -> float:
        try:
            value = float(raw[key])
        except (KeyError, TypeError, ValueError):
            raise LibraryError(f"Thiếu hoặc sai {label}.") from None
        if not math.isfinite(value):
            raise LibraryError(f"Sai {label}.")
        return value

    heading = round(num("heading", "hướng"), 3) % 360
    scale = num("scale", "tỉ lệ")
    if not 0 < scale <= MAX_SCALE:
        raise LibraryError(f"Tỉ lệ phải lớn hơn 0 và không quá {MAX_SCALE:g}.")
    anchor = raw.get("anchor")
    if anchor not in ANCHORS:
        raise LibraryError("Điểm neo trên mô hình phải là gốc GLB hoặc tâm đáy hộp bao.")
    lon, lat = num("lon", "kinh độ"), num("lat", "vĩ độ")
    if not (-180 <= lon <= 180 and -85 <= lat <= 85):
        raise LibraryError("Toạ độ neo không hợp lệ.")
    return {"heading": heading, "scale": scale, "anchor": anchor, "lon": round(lon, 8), "lat": round(lat, 8)}


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def usage(rec: dict[str, Any]) -> dict[int, list[list[str | None]]]:
    """Version -> the periods [from, to] it was in use (to = None: still in use)."""
    periods: dict[int, list[list[str | None]]] = {}
    current: tuple[int, str] | None = None
    for ev in rec["history"]:
        if ev["action"] != "use":
            continue
        if current:
            periods.setdefault(current[0], []).append([current[1], ev["at"]])
        current = (ev["to"], ev["at"]) if ev["to"] is not None else None
    if current:
        periods.setdefault(current[0], []).append([current[1], None])
    return periods


def view(rec: dict[str, Any]) -> dict[str, Any]:
    """A landmark as the UI shows it: each version gets its status and periods of use."""
    periods = usage(rec)
    versions = []
    for v in rec["versions"]:
        n = v["version"]
        status = "in_use" if n == rec["in_use"] else "used" if n in periods else "unused"
        versions.append({**v, "status": status, "used": periods.get(n, [])})
    return {**rec, "versions": versions}


def summary(rec: dict[str, Any]) -> dict[str, Any]:
    fp = rec["footprint"]
    return {
        "id": rec["id"],
        "building_id": rec["building_id"],
        "name": rec["name"],
        "link": rec["link"],
        "in_use": rec["in_use"],
        "versions": len(rec["versions"]),
        "centroid": fp["centroid"],
        "area_m2": fp["area_m2"],
        "height": fp["height"],
        "geometry": fp["geometry"],
        "source_name": (rec.get("source") or {}).get("name") or "",
        "updated_at": rec["updated_at"],
    }


def same_file(recorded: Any, path: Path) -> bool:
    """Whether the PMTiles a landmark was registered from (source.path) is this file. Both sides
    are resolved, so a symlink or a relative path to the same file still matches."""
    if not recorded:
        return False
    try:
        return Path(str(recorded)).expanduser().resolve() == path.resolve()
    except (OSError, RuntimeError):  # unreadable path, symlink loop
        return False


def ranges_overlap(a: list[Any], b: list[Any]) -> bool:
    """Whether two zoom ranges [min, max | None) share a zoom."""
    inf = math.inf
    return a[0] < (b[1] if b[1] is not None else inf) and b[0] < (a[1] if a[1] is not None else inf)


def zoom_label(r: list[Any]) -> str:
    return f"≥ {r[0]:g}" if r[1] is None else f"{r[0]:g}–{r[1]:g}"


def slot_lod(slot: Any) -> dict[str, Any] | None:
    """The LOD of an upload slot ("15", "16" or "17"), None for no slot."""
    if slot is None or slot == "":
        return None
    try:
        z = int(slot)
    except (TypeError, ValueError):
        z = None
    if z not in LOD_SLOTS:
        raise LibraryError(f"Không có ô zoom z{slot}: chỉ có {', '.join(f'z{k}' for k in sorted(LOD_SLOTS))}.")
    return {"lod_zoom": z, "zoom_range": list(LOD_SLOTS[z])}


def anchor_distance(a: dict[str, float], b: dict[str, float]) -> float:
    """Ground distance in metres between two {lat, lon} (short distances)."""
    k = math.cos(math.radians(a["lat"]))
    return math.hypot((b["lon"] - a["lon"]) * k, b["lat"] - a["lat"]) * 111_320.0


def version_lods(v: dict[str, Any]) -> list[dict[str, Any]]:
    """Every file of a version with a zoom range, most detailed first (`own`: the version's own
    file). Empty when its own file has no range: it is drawn at every zoom."""
    own = (v.get("model") or {}).get("lod")
    if not own:
        return []
    files = [{**own, "own": True, "file": v["file"], "original_name": v["original_name"], "size": v["size"],
              "sha256": v["sha256"]}]
    files += [{"lod_zoom": x["lod_zoom"], "zoom_range": x["zoom_range"], "own": False, "file": x["file"],
               "original_name": x["original_name"], "size": x["size"], "sha256": x["sha256"]} for x in v.get("lods", [])]
    return sorted(files, key=lambda x: x["zoom_range"][0], reverse=True)


def render_item(rec: dict[str, Any]) -> dict[str, Any]:
    """What the 3D preview needs to draw a landmark over its building: the version in use, or the
    newest version while none is in use (`in_use` tells which)."""
    fp, src = rec["footprint"], rec.get("source") or {}
    v = Library.version(rec, rec["in_use"]) if rec["in_use"] is not None else rec["versions"][-1]
    return {
        "id": rec["id"],
        "building_id": rec["building_id"],
        "name": rec["name"],
        "geometry": fp["geometry"],
        "centroid": fp["centroid"],
        "bbox": fp["bbox"],
        "height": fp["height"],
        "layer": src.get("layer"),
        "id_field": src.get("id_field"),
        "parent_field": src.get("parent_field"),
        "version": v["version"],
        "in_use": rec["in_use"],
        "original_name": v["original_name"],
        "sha256": v["sha256"],
        "size": v["size"],
        "model": v["model"],
        "placement": v["placement"],
        "lods": version_lods(v),
    }


class Library:
    def __init__(self, root: Path) -> None:
        self.root = root
        self._lock = threading.RLock()

    # ------------------------------------------------------------------ reading

    def folder(self, lid: str) -> Path:
        if not lid or _ID_RE.sub("_", lid) != lid or lid.startswith((".", "_")):
            raise NotFound("Không có landmark này.")
        path = self.root / lid
        if not (path / MANIFEST).is_file():
            raise NotFound("Không có landmark này.")
        return path

    def read(self, lid: str) -> dict[str, Any]:
        return json.loads((self.folder(lid) / MANIFEST).read_text("utf-8"))

    def _write(self, rec: dict[str, Any]) -> None:
        path = self.root / rec["id"]
        tmp = path / f".{MANIFEST}.tmp"
        tmp.write_text(json.dumps(rec, ensure_ascii=False, indent=2) + "\n", "utf-8")
        os.replace(tmp, path / MANIFEST)

    def _records(self) -> list[dict[str, Any]]:
        if not self.root.is_dir():
            return []
        out = []
        for path in sorted(self.root.iterdir()):
            if path.name.startswith((".", "_")) or not (path / MANIFEST).is_file():
                continue
            try:
                out.append(json.loads((path / MANIFEST).read_text("utf-8")))
            except (OSError, ValueError):
                continue  # half-copied folder: the rest of the library still lists
        return out

    def list(self) -> list[dict[str, Any]]:
        recs = self._records()
        recs.sort(key=lambda r: (r["name"] or r["building_id"]).casefold())
        return [summary(r) for r in recs]

    def get(self, lid: str) -> dict[str, Any]:
        return view(self.read(lid))

    def glb_path(self, lid: str, version: int, lod_zoom: float | None = None) -> tuple[Path, dict[str, Any]]:
        """The .glb of a version (its own file, or the file of one zoom range) and its record."""
        rec = self.read(lid)
        v = self.version(rec, version)
        item = v
        if lod_zoom is not None and lod_zoom != ((v.get("model") or {}).get("lod") or {}).get("lod_zoom"):
            item = next((x for x in v.get("lods", []) if x["lod_zoom"] == lod_zoom), None)
            if item is None:
                raise NotFound(f"v{version} không có file cho zoom {lod_zoom:g}.")
        path = self.folder(lid) / item["file"]
        if not path.is_file():
            raise NotFound(f"Thiếu file {item['file']} trong thư mục landmark.")
        return path, item

    def sources(self) -> list[dict[str, Any]]:
        """The PMTiles landmarks were registered from, newest first, while they still exist."""
        seen: dict[str, dict[str, Any]] = {}
        for rec in sorted(self._records(), key=lambda r: r["created_at"], reverse=True):
            src = rec.get("source") or {}
            if src.get("path") and src["path"] not in seen and Path(src["path"]).is_file():
                seen[src["path"]] = src
        return list(seen.values())

    def render(self, path: Path) -> list[dict[str, Any]]:
        """The landmarks registered from this PMTiles that have a GLB, by name: the version in use
        is drawn, else the newest version (the manifest keeps the versions in use only)."""
        recs = [r for r in self._records()
                if r["versions"] and same_file((r.get("source") or {}).get("path"), path)]
        recs.sort(key=lambda r: (r["name"] or r["building_id"]).casefold())
        return [render_item(r) for r in recs]

    def released(self, path: Path) -> list[dict[str, Any]]:
        """The landmarks registered from this PMTiles with a version in use (records): those
        cast their ground shadow from the GLB (app/landmark_shadow.py)."""
        return [r for r in self._records()
                if r["in_use"] is not None and same_file((r.get("source") or {}).get("path"), path)]

    def source_paths(self) -> dict[str, str]:
        """Landmark id -> the PMTiles it was registered from."""
        return {r["id"]: (r.get("source") or {}).get("path") or "" for r in self._records()}

    def manifest(self) -> dict[str, Any]:
        """The version in use of every landmark, with its placement: the file to hand over."""
        items = []
        for rec in sorted(self._records(), key=lambda r: r["id"]):
            if rec["in_use"] is None:
                continue
            v = self.version(rec, rec["in_use"])
            items.append({
                "id": rec["id"],
                "building_id": rec["building_id"],
                "name": rec["name"],
                "link": rec["link"],
                "version": v["version"],
                "file": f"{rec['id']}/{v['file']}",
                "sha256": v["sha256"],
                "size": v["size"],
                "placement": v["placement"],
                "footprint_centroid": rec["footprint"]["centroid"],
                "building_height": rec["footprint"]["height"],
                "lods": [{"lod_zoom": x["lod_zoom"], "zoom_range": x["zoom_range"], "file": f"{rec['id']}/{x['file']}",
                          "sha256": x["sha256"], "size": x["size"]} for x in version_lods(v)],
            })
        return {"generated_at": now(), "heading": "degrees clockwise seen from above; 0 = GLB +X east, front (+Z) south",
                "lods": "files of one version per zoom range [min, max): draw the one whose range holds the zoom, "
                        "none outside every range; empty: `file` at every zoom",
                "landmarks": items}

    @staticmethod
    def version(rec: dict[str, Any], version: int) -> dict[str, Any]:
        for v in rec["versions"]:
            if v["version"] == version:
                return v
        raise NotFound(f"Landmark không có version v{version}.")

    # ------------------------------------------------------------------ writing

    def _new_id(self, building_id: str) -> str:
        base = _ID_RE.sub("_", building_id).strip("._") or "landmark"
        if base.startswith(("_", ".")) or base in RESERVED_IDS:
            base = f"b{base}"
        base = base[:80]
        lid, n = base, 1
        while (self.root / lid).exists():
            n += 1
            lid = f"{base}-{n}"
        return lid

    def _touch(self, rec: dict[str, Any], event: dict[str, Any]) -> None:
        t = now()
        rec["updated_at"] = t
        rec["history"].append({"at": t, "by": user(), **event})

    def register(self, *, building_id: str, name: str, link: str, footprint: dict[str, Any],
                 source: dict[str, Any]) -> dict[str, Any]:
        building_id = _text(building_id, "building_id")
        if not building_id:
            raise LibraryError("Thiếu building_id.")
        name, link = _text(name, "Tên"), _text(link, "ID liên kết")
        with self._lock:
            for rec in self._records():
                if rec["building_id"] == building_id:
                    raise LibraryError(f"Building {building_id} đã có trong thư viện ({rec['name'] or rec['id']}).")
            lid = self._new_id(building_id)
            (self.root / lid).mkdir(parents=True)
            t = now()
            rec = {
                "schema": SCHEMA, "id": lid, "building_id": building_id, "name": name, "link": link,
                "footprint": footprint, "source": source, "created_at": t, "updated_at": t,
                "in_use": None, "versions": [], "history": [{"at": t, "by": user(), "action": "register"}],
            }
            self._write(rec)
        return view(rec)

    def update(self, lid: str, *, name: Any = None, link: Any = None) -> dict[str, Any]:
        with self._lock:
            rec = self.read(lid)
            changed = []
            for key, value, label in (("name", name, "Tên"), ("link", link, "ID liên kết")):
                if value is not None and _text(value, label) != rec[key]:
                    rec[key] = _text(value, label)
                    changed.append(key)
            if changed:
                self._touch(rec, {"action": "edit", "fields": changed})
                self._write(rec)
        return view(rec)

    def add_version(self, lid: str, upload: Path, *, original_name: str, note: str, slot: Any = None) -> dict[str, Any]:
        """Keep an uploaded .glb (a file inside the landmark folder) as the next version (slot: the
        zoom slot it was uploaded into, whose range replaces the GLB's)."""
        note = _text(note, "Ghi chú")
        model = glb.inspect(upload)
        if not model["meshes"]:
            raise LibraryError("GLB không có mesh nào.")
        model["lod"] = slot_lod(slot) or model.get("lod")
        digest, size = sha256(upload), upload.stat().st_size
        with self._lock:
            rec = self.read(lid)
            n = max((v["version"] for v in rec["versions"]), default=0) + 1
            anchor = model.get("anchor")
            if anchor:  # a georeferenced model: no turn, its origin at its anchor
                placement = {"heading": 0.0, "scale": 1.0, "anchor": "origin",
                             "lon": round(anchor["lon"], 8), "lat": round(anchor["lat"], 8)}
            elif rec["in_use"] is not None:
                placement = dict(self.version(rec, rec["in_use"])["placement"])
            elif rec["versions"]:
                placement = dict(rec["versions"][-1]["placement"])
            else:
                lon, lat = rec["footprint"]["centroid"]
                placement = {"heading": 0.0, "scale": 1.0, "anchor": "origin", "lon": lon, "lat": lat}
            file = f"v{n}.glb"
            os.replace(upload, self.folder(lid) / file)
            rec["versions"].append({
                "version": n, "file": file, "original_name": _text(original_name, "Tên file") or file,
                "size": size, "sha256": digest, "uploaded_at": now(), "by": user(), "note": note,
                "same_as": next((v["version"] for v in rec["versions"] if v["sha256"] == digest), None),
                "model": model, "placement": placement,
            })
            self._touch(rec, {"action": "upload", "version": n, "note": note})
            self._write(rec)
        return view(rec)

    def add_lod(self, lid: str, version: int, upload: Path, *, original_name: str, slot: Any = None) -> dict[str, Any]:
        """Keep an uploaded .glb (a file inside the landmark folder) as another level of detail of
        a version: its zoom_range (the slot's, else the GLB's) must not overlap the version's other
        files."""
        model = glb.inspect(upload)
        if not model["meshes"]:
            raise LibraryError("GLB không có mesh nào.")
        model["lod"] = slot_lod(slot) or model.get("lod")
        lod = model["lod"]
        if not lod:
            raise LibraryError(f"{original_name or 'File'} không có asset.extras.zoom_range nên không biết vẽ ở zoom nào.")
        digest, size = sha256(upload), upload.stat().st_size
        with self._lock:
            rec = self.read(lid)
            v = self.version(rec, version)
            own = (v.get("model") or {}).get("lod")
            if not own:
                raise LibraryError(f"File của v{version} không có zoom_range: không thêm file theo zoom vào version này được.")
            for other in [{**own, "file": v["file"]}, *v.get("lods", [])]:
                if other["lod_zoom"] == lod["lod_zoom"] or ranges_overlap(other["zoom_range"], lod["zoom_range"]):
                    raise LibraryError(f"Zoom {zoom_label(lod['zoom_range'])} trùng với file z{other['lod_zoom']} "
                                       f"({zoom_label(other['zoom_range'])}) của v{version}.")
            a, b = (v.get("model") or {}).get("anchor"), model.get("anchor")
            if a and b and anchor_distance(a, b) > 1:
                raise LibraryError(f"Anchor của file lệch {anchor_distance(a, b):.1f} m so với file của v{version}: "
                                   "các file theo zoom phải cùng một gốc.")
            file = f"v{version}_z{str(lod['lod_zoom']).replace('.', '_')}.glb"
            os.replace(upload, self.folder(lid) / file)
            v.setdefault("lods", []).append({
                **lod, "file": file, "original_name": _text(original_name, "Tên file") or file,
                "size": size, "sha256": digest, "uploaded_at": now(), "by": user(), "model": model,
            })
            v["lods"].sort(key=lambda x: x["zoom_range"][0], reverse=True)
            self._touch(rec, {"action": "lod", "version": version, "lod_zoom": lod["lod_zoom"]})
            self._write(rec)
        return view(rec)

    def set_placement(self, lid: str, version: int, raw: dict[str, Any]) -> dict[str, Any]:
        placement = check_placement(raw)
        with self._lock:
            rec = self.read(lid)
            v = self.version(rec, version)
            if v["placement"] != placement:
                v["placement"] = placement
                self._touch(rec, {"action": "placement", "version": version})
                self._write(rec)
        return view(rec)

    def use(self, lid: str, version: int | None, note: str = "") -> dict[str, Any]:
        """Put one version in use (None: none). The switch goes to the history."""
        note = _text(note, "Ghi chú")
        with self._lock:
            rec = self.read(lid)
            if version is not None:
                self.version(rec, version)
            if version == rec["in_use"]:
                raise LibraryError("Version này đang được dùng rồi." if version is not None else "Chưa dùng version nào.")
            self._touch(rec, {"action": "use", "from": rec["in_use"], "to": version, "note": note})
            rec["in_use"] = version
            self._write(rec)
        return view(rec)

    def trash(self, lid: str) -> Path:
        """Move a landmark folder to _trash/ (nothing is erased)."""
        with self._lock:
            src = self.folder(lid)
            dest = self.root / TRASH / f"{lid}__{datetime.now().strftime('%Y%m%d-%H%M%S')}"
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dest))
        return dest
