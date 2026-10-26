"""Landmark library: the GLB files of each landmark and the versions in use, kept on disk.

    landmarks/                    (BDS_LANDMARK_DIR)
      <id>/landmark.json          building, footprint, versions, placements, history
      <id>/v1.glb, v2.glb, …      every upload, never overwritten
      _trash/<id>__<time>/        deleted landmarks (moved, not erased)

A landmark replaces the extrusion of one building_id. Its footprint is read once, when it is
registered, from the PMTiles it was clicked on (landmark.lookup), so the library opens without
that file. `link` is a free id to find the building elsewhere (MapOps canonical id, OSM id…).

Each version has its own placement, since a new export may move the model's origin: heading
(degrees clockwise seen from above, as in the 3D preview), scale, the model point that goes on
the ground (GLB origin or bottom centre of its box) and the lon / lat it goes to. A new version
starts from the placement of the version in use. One version at a time is in use; every switch
is kept in the history, so the versions that were used, and when, can be read back.
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

    def glb_path(self, lid: str, version: int) -> tuple[Path, dict[str, Any]]:
        rec = self.read(lid)
        v = self.version(rec, version)
        path = self.folder(lid) / v["file"]
        if not path.is_file():
            raise NotFound(f"Thiếu file {v['file']} trong thư mục landmark.")
        return path, v

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
            })
        return {"generated_at": now(), "heading": "degrees clockwise seen from above; 0 = GLB +X east, front (+Z) south",
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

    def add_version(self, lid: str, upload: Path, *, original_name: str, note: str) -> dict[str, Any]:
        """Keep an uploaded .glb (a file inside the landmark folder) as the next version."""
        note = _text(note, "Ghi chú")
        model = glb.inspect(upload)
        if not model["meshes"]:
            raise LibraryError("GLB không có mesh nào.")
        digest, size = sha256(upload), upload.stat().st_size
        with self._lock:
            rec = self.read(lid)
            n = max((v["version"] for v in rec["versions"]), default=0) + 1
            if rec["in_use"] is not None:
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
