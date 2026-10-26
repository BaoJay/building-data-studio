"""HTTP server: JSON API + static UI. Binds to 127.0.0.1 only."""

from __future__ import annotations

import argparse
import logging
import os
import secrets
import subprocess
import sys
import threading
import webbrowser
from pathlib import Path
from typing import Any
from urllib.parse import quote

import uvicorn
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from . import __version__, diff, glb, landmark, landmark_library, landmark_shadow, osmbase, pipeline, probe, shadows, tools, vmap
from .jobs import FileRegistry, Job, JobManager

log = logging.getLogger(__name__)

APP_DIR = Path(__file__).resolve().parent
STATIC_DIR = APP_DIR / "static"
UPLOAD_DIR = APP_DIR.parent / "uploads"
DEFAULT_PORT = 8765
HOST = "127.0.0.1"
ALLOWED_HOSTS = {"127.0.0.1", "localhost"}

registry = FileRegistry()
library = landmark_library.Library(landmark_library.default_dir())
BASE_DIR = osmbase.default_dir()


def execute(job: Job) -> None:
    """Run a job of either kind on the worker thread."""
    if job.kind == "diff":
        diff.execute(job, registry)
    elif job.kind == "base":
        osmbase.execute(job, registry, BASE_DIR)
    else:
        pipeline.execute(job, registry)


jobs = JobManager(execute=execute)


class LocalOnlyMiddleware:
    """Reject requests not addressed to localhost (DNS rebinding) or sent cross-site.

    Pure ASGI (not BaseHTTPMiddleware) so streamed uploads pass through untouched.
    """

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] == "http":
            headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope["headers"]}
            reason = _forbidden_reason(scope["method"], headers.get("host", ""), headers.get("origin"))
            if reason:
                await JSONResponse({"error": reason}, status_code=403)(scope, receive, send)
                return
        await self.app(scope, receive, send)


def _hostname(value: str) -> str:
    """'localhost:8765' / 'http://127.0.0.1:8765' / '[::1]:80' -> bare host name."""
    value = value.split("://", 1)[-1].split("/", 1)[0]
    if value.startswith("["):
        return value[1:].split("]", 1)[0]
    return value.rsplit(":", 1)[0] if value.count(":") == 1 else value


def _forbidden_reason(method: str, host: str, origin: str | None) -> str | None:
    if _hostname(host) not in ALLOWED_HOSTS:
        return "Forbidden host"
    if method not in ("GET", "HEAD") and origin and _hostname(origin) not in ALLOWED_HOSTS:
        return "Forbidden origin"
    return None


class RevalidatingStaticFiles(StaticFiles):
    """Static files that the browser must revalidate (ETag -> 304), so UI updates show up at once."""

    async def get_response(self, path: str, scope) -> Response:
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache"
        return response


NO_CACHE = {"Cache-Control": "no-cache"}


def _error(message: str, status: int = 400) -> JSONResponse:
    return JSONResponse({"error": message}, status_code=status)


async def index(request: Request) -> Response:
    return FileResponse(STATIC_DIR / "index.html", headers=NO_CACHE)


async def preview_page(request: Request) -> Response:
    return FileResponse(STATIC_DIR / "preview.html", headers=NO_CACHE)


async def health(request: Request) -> Response:
    versions = tools.tool_versions()
    missing = [name for name in tools.REQUIRED_TOOLS if not versions[name]["path"]]
    return JSONResponse({
        "version": __version__,
        "tools": versions,
        "missing": missing,
        "defaults": {"output_dir": str(pipeline.DEFAULT_OUTPUT_DIR)},
        "accept": sorted(probe.ALLOWED_SUFFIXES),
    })


async def upload(request: Request) -> Response:
    """Stream the request body to uploads/<random>/<name> (no size limit, no RAM copy)."""
    raw_name = request.query_params.get("name", "")
    name = pipeline.safe_name(Path(raw_name).name)
    suffix = Path(raw_name).suffix.lower()
    if suffix not in probe.ALLOWED_SUFFIXES:
        return _error(f"Định dạng {suffix or '(không có đuôi)'} chưa hỗ trợ.")
    if not name.lower().endswith(suffix):
        name += suffix
    dest_dir = UPLOAD_DIR / secrets.token_hex(4)
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / name
    size = 0
    try:
        with dest.open("wb") as fh:
            async for chunk in request.stream():
                fh.write(chunk)
                size += len(chunk)
    except OSError as exc:
        dest.unlink(missing_ok=True)
        return _error(f"Không ghi được file upload: {exc}", 500)
    log.info("Uploaded %s (%d bytes)", dest, size)
    return JSONResponse({"path": str(dest), "size": size})


async def probe_file(request: Request) -> Response:
    body = await _json(request)
    try:
        path = probe.resolve_input_path(str(body.get("path", "")))
        result = await _in_thread(probe.probe, path, body.get("geom_column") or None, body.get("layer") or None)
    except (probe.ProbeError, tools.ToolMissingError) as exc:
        return _error(str(exc))
    result["token"] = registry.register(path)
    return JSONResponse(result)


async def create_job(request: Request) -> Response:
    body = await _json(request)
    kind = body.get("kind") or "convert"
    if kind not in ("convert", "diff"):
        return _error(f"Loại job không hợp lệ: {kind}")
    try:
        cfg = diff.parse_config(body) if kind == "diff" else pipeline.parse_config(body)
    except pipeline.ConfigError as exc:
        return _error(str(exc))
    job = jobs.submit(body, name=cfg.out_name, kind=kind)
    return JSONResponse({"id": job.id}, status_code=201)


async def list_jobs(request: Request) -> Response:
    return JSONResponse([job.summary() for job in jobs.list()])


async def get_job(request: Request) -> Response:
    job = jobs.get(request.path_params["job_id"])
    if job is None:
        return _error("Không tìm thấy job.", 404)
    since = int(request.query_params.get("since", 0) or 0)
    return JSONResponse(job.to_dict(log_since=since))


async def cancel_job(request: Request) -> Response:
    ok = jobs.cancel(request.path_params["job_id"])
    return JSONResponse({"cancelled": ok})


async def get_file(request: Request) -> Response:
    """Serve a registered file. Range requests are supported (needed by PMTiles)."""
    path = registry.get(request.path_params["token"])
    if path is None or not path.is_file():
        return _error("File không tồn tại.", 404)
    if request.query_params.get("download"):
        return FileResponse(path, filename=path.name)
    media = "application/octet-stream"
    if path.suffix == ".json":
        media = "application/json"
    elif path.suffix in (".md", ".txt"):
        media = "text/plain; charset=utf-8"
    return FileResponse(path, media_type=media)


async def reveal(request: Request) -> Response:
    """Open a registered folder, or reveal a registered file, in the OS file manager."""
    body = await _json(request)
    path = registry.get(str(body.get("token", "")))
    if path is None or not path.exists():
        return _error("Không tìm thấy đường dẫn.", 404)
    try:
        open_in_file_manager(path)
    except (OSError, subprocess.SubprocessError) as exc:
        return _error(f"Không mở được: {exc}", 500)
    return JSONResponse({"ok": True, "path": str(path)})


async def vmap_presets(request: Request) -> Response:
    return JSONResponse(vmap.presets_info(), headers=NO_CACHE)


async def vmap_style(request: Request) -> Response:
    """The mobile basemap in one light preset. ?download=1 keeps the assets.vmaps.vn URLs
    (a file to hand to the mobile app); otherwise sprite / glyphs go through vmap_asset."""
    preset = request.query_params.get("preset") or vmap.DEFAULT_PRESET
    if preset not in vmap.PRESETS:
        return _error(f"Không có preset {preset}.")
    download = bool(request.query_params.get("download"))
    asset_base = None if download else f"{request.base_url}api/vmap/assets/"
    body, source = await _in_thread(vmap.style_bytes, preset, asset_base)
    headers = {**NO_CACHE, "X-VMAP-Source": source}
    if download:
        headers["Content-Disposition"] = f'attachment; filename="{vmap.PRESETS[preset].style}.style.json"'
    return Response(body, media_type="application/json", headers=headers)


async def vmap_asset(request: Request) -> Response:
    """Same-origin copy of a sprite / glyph file from assets.vmaps.vn (it sends no CORS headers)."""
    try:
        body, media = await _in_thread(vmap.fetch_asset, request.path_params["path"])
    except vmap.AssetNotAllowed:
        return _error("Không hỗ trợ đường dẫn này.", 404)
    except vmap.UpstreamError as exc:
        return _error(f"Không tải được từ assets.vmaps.vn: {exc}", 502)
    return Response(body, media_type=media, headers={"Cache-Control": "max-age=86400"})


# ------------------------------------------------------------------ base (OSM buildings of a city)

def base_status(area_key: str = osmbase.DEFAULT_AREA) -> dict[str, Any]:
    """The base in use (with file tokens) and the latest build job of this session."""
    area = osmbase.AREAS[area_key]
    out: dict[str, Any] = {"area": area.key, "label": area.label, "dir": str(osmbase.area_dir(BASE_DIR, area)),
                           "ready": False, "job": None}
    manifest = osmbase.read_manifest(BASE_DIR, area)
    if manifest:
        out.update(ready=True, base={
            **{k: v for k, v in manifest.items() if not k.endswith("_path")},
            "pmtiles_token": registry.register(Path(manifest["pmtiles_path"])),
            "gpkg_token": registry.register(Path(manifest["gpkg_path"])),
            "folder_token": registry.register(Path(manifest["folder_path"])),
        })
    job = _base_job(area.key)
    if job is not None:
        d = job.to_dict(log_since=1 << 30)
        out["job"] = {k: d[k] for k in ("id", "status", "error", "steps", "started", "ended")}
    return out


def _base_job(area_key: str) -> Job | None:
    return next((j for j in jobs.list() if j.kind == "base" and j.config.get("area") == area_key), None)


def start_base_build(area_key: str = osmbase.DEFAULT_AREA, release: str | None = None) -> Job:
    """Queue a build of the base, unless one is already queued or running."""
    job = _base_job(area_key)
    if job is not None and job.status in ("queued", "running"):
        return job
    area = osmbase.AREAS[area_key]
    return jobs.submit({"area": area.key, "release": release}, name=osmbase.out_name(area), kind="base")


async def get_base(request: Request) -> Response:
    area = request.query_params.get("area") or osmbase.DEFAULT_AREA
    if area not in osmbase.AREAS:
        return _error(f"Không có khu vực {area}.", 404)
    return JSONResponse(base_status(area), headers=NO_CACHE)


async def build_base(request: Request) -> Response:
    body = await _json(request)
    area = str(body.get("area") or osmbase.DEFAULT_AREA)
    if area not in osmbase.AREAS:
        return _error(f"Không có khu vực {area}.", 404)
    release = str(body.get("release") or "").strip() or None
    job = start_base_build(area, release)
    return JSONResponse({"id": job.id, **base_status(area)}, status_code=202)


async def building_shadows(request: Request) -> Response:
    """Prototype: dissolved ground shadows of the buildings of a registered PMTiles over one
    area, cast along the light of a VMAP preset. ?download=1 saves the GeoJSON."""
    q = request.query_params
    path = registry.get(q.get("f", ""))
    if path is None or not path.is_file():
        return _error("File không tồn tại.", 404)
    preset = q.get("preset") or vmap.DEFAULT_PRESET
    if preset not in vmap.PRESETS:
        return _error(f"Không có preset {preset}.")
    try:
        if vmap.PRESETS[preset].shadow is None:
            raise shadows.ShadowError("Chế độ này không có nắng nên không đổ bóng.")
        height = shadows.check_field(q.get("h"), "chiều cao")
        if not height:
            raise shadows.ShadowError("Thiếu cột chiều cao.")
        light = await _in_thread(vmap.effective_light, preset)
        if not light:
            raise shadows.ShadowError("Style không có khối light để lấy hướng nắng.")
        zoom = int(q["z"]) if q.get("z") and path.suffix.lower() == ".pmtiles" else None
        layer = shadows.check_field(q.get("layer"), "layer") or ""
        # Released landmarks of this file cast from their GLB instead of their LoD1 box.
        landmarks, fallbacks = await _in_thread(landmark_shadow.released, library, path, preset)
        req = shadows.Request(
            path=path,
            layer=layer,
            zoom=zoom,
            bbox=shadows.parse_bbox(q.get("bbox", "")),
            height=height,
            base=shadows.check_field(q.get("base"), "đáy"),
            parts=shadows.check_field(q.get("parts"), "has_parts"),
            sun=shadows.Sun.from_light(light),
            landmarks=tuple(lm for lm in landmarks if lm.layer in ("", layer)),
        )
        body, stats = await _in_thread(shadows.cached_compute, req)
    except ValueError as exc:  # ShadowError, bad z
        return _error(str(exc))
    except tools.ToolMissingError as exc:
        return _error(str(exc), 500)
    headers = {
        **NO_CACHE,
        "X-Shadow-Buildings": str(stats["buildings"]),
        "X-Shadow-Ms": str(stats["ms"]),
        "X-Sun-Azimuth": f"{req.sun.azimuth:g}",
        "X-Sun-Altitude": f"{req.sun.altitude:g}",
        "X-Shadow-Landmarks": str(stats["landmarks"]),
    }
    if fallbacks:  # landmarks that keep their LoD1 shadow, and why (UTF-8, percent-encoded)
        headers["X-Shadow-Landmark-Errors"] = quote(" · ".join(fallbacks)[:2000])
    if q.get("download"):
        headers["Content-Disposition"] = f'attachment; filename="shadows-{preset}.geojson"'
    return Response(body, media_type="application/geo+json", headers=headers)


async def building_footprint(request: Request) -> Response:
    """Footprint centroid, size and height of one building of a registered PMTiles, read
    around ?lon=&lat=: where the 3D preview anchors a landmark model (app/landmark.py)."""
    q = request.query_params
    path = registry.get(q.get("f", ""))
    if path is None or not path.is_file():
        return _error("File không tồn tại.", 404)
    try:
        result = await _in_thread(landmark.lookup, _lookup_request(path, q))
    except ValueError as exc:  # LandmarkError, ShadowError, bad z / lon / lat
        return _error(str(exc))
    except tools.ToolMissingError as exc:
        return _error(str(exc), 500)
    return JSONResponse(result, headers=NO_CACHE)


def _lookup_request(path: Path, q) -> landmark.Lookup:
    """The building to read, from query parameters or a JSON body (same keys)."""
    id_field = shadows.check_field(q.get("idcol"), "id")
    building_id = str(q.get("id") or "").strip()
    if not id_field or not building_id:
        raise landmark.LandmarkError("Thiếu building_id.")
    req = landmark.Lookup(
        path=path,
        layer=shadows.check_field(q.get("layer"), "layer") or "",
        zoom=int(q["z"]) if q.get("z") is not None and q.get("z") != "" and path.suffix.lower() == ".pmtiles" else None,
        id_field=id_field,
        building_id=building_id,
        lon=_coord(q.get("lon")),
        lat=_coord(q.get("lat")),
        height=shadows.check_field(q.get("h"), "chiều cao"),
        base=shadows.check_field(q.get("base"), "đáy"),
        parent=shadows.check_field(q.get("parent"), "building cha"),
    )
    if not (-180 <= req.lon <= 180 and -85 <= req.lat <= 85):
        raise landmark.LandmarkError("Toạ độ không hợp lệ.")
    return req


def _coord(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        raise landmark.LandmarkError("Thiếu toạ độ lon / lat.") from None


# ------------------------------------------------------------------ landmark library

MAX_GLB_BYTES = 1 << 30


def _landmark_json(rec: dict[str, Any], status: int = 200) -> JSONResponse:
    """A landmark plus the token that reveals its folder in the file manager."""
    folder = library.root / rec["id"]
    return JSONResponse({**rec, "folder_token": registry.register(folder)}, status_code=status, headers=NO_CACHE)


def _library_error(exc: ValueError) -> JSONResponse:
    return _error(str(exc), 404 if isinstance(exc, landmark_library.NotFound) else 400)


def _source_file(token: str) -> Path | None:
    """The PMTiles behind a file token, or None when the token is unknown or its file is gone."""
    path = registry.get(token)
    return path if path is not None and path.is_file() else None


async def list_landmarks(request: Request) -> Response:
    """Every landmark (with its footprint) and the PMTiles they were registered from.

    With ?f=<token>, each landmark also says whether it was registered from that PMTiles
    (`same_source`) and, when its own PMTiles still exists, the token that opens it."""
    library.root.mkdir(parents=True, exist_ok=True)
    items = await _in_thread(library.list)
    sources = [{**s, "token": registry.register(Path(s["path"]))} for s in await _in_thread(library.sources)]
    if request.query_params.get("f") is not None:
        current = _source_file(request.query_params["f"])
        paths = await _in_thread(library.source_paths)
        tokens = {s["path"]: s["token"] for s in sources}
        for item in items:
            path = paths.get(item["id"], "")
            item["same_source"] = current is not None and landmark_library.same_file(path, current)
            item["source_token"] = tokens.get(path)
    base = (await _in_thread(base_status)).get("base")
    if base:
        # The base comes first: the 3D preview opens it when no file is given.
        token = base["pmtiles_token"]
        sources = [{"token": token, "name": base["name"], "layer": "buildings", "id_field": "building_id",
                    "base": True, "view": base["view"]}, *(s for s in sources if s["token"] != token)]
    return JSONResponse({"dir": str(library.root), "dir_token": registry.register(library.root),
                         "landmarks": items, "sources": sources}, headers=NO_CACHE)


async def render_landmarks(request: Request) -> Response:
    """The landmarks of one PMTiles (?f=<token>) that have a GLB, for the 3D preview: footprint,
    the version to draw (in use, else the newest), placement, model box and GLB URL. Landmarks
    registered from another file are left out: the token is resolved to its path and compared
    with each source.path."""
    path = _source_file(request.query_params.get("f", ""))
    if path is None:
        return _error("File PMTiles không tồn tại.", 404)
    items = await _in_thread(library.render, path)
    for item in items:
        item["glb"] = f"/api/landmarks/{item['id']}/versions/{item['version']}/glb"
    return JSONResponse({"source": {"name": path.name, "token": request.query_params["f"]}, "landmarks": items},
                        headers=NO_CACHE)


async def register_landmark(request: Request) -> Response:
    """Add a building of a registered PMTiles to the library: its footprint is read once, here."""
    body = await _json(request)
    path = registry.get(str(body.get("f", "")))
    if path is None or not path.is_file():
        return _error("File PMTiles không tồn tại.", 404)
    try:
        req = _lookup_request(path, {k: v if v is None else str(v) for k, v in body.items()})
        found = await _in_thread(landmark.lookup, req, True)
        footprint = {k: found[k] for k in ("geometry", "centroid", "area_m2", "bbox", "size_m", "height", "base",
                                           "matched", "pieces", "height_field")}
        source = {"name": path.name, "path": str(path), "layer": req.layer, "id_field": req.id_field,
                  "zoom": req.zoom, "parent_field": req.parent}
        rec = await _in_thread(lambda: library.register(building_id=req.building_id, name=str(body.get("name") or ""),
                                                        link=str(body.get("link") or ""), footprint=footprint, source=source))
    except ValueError as exc:  # LandmarkError, LibraryError, ShadowError
        return _error(str(exc))
    except tools.ToolMissingError as exc:
        return _error(str(exc), 500)
    return _landmark_json(rec, 201)


async def get_landmark(request: Request) -> Response:
    try:
        rec = await _in_thread(library.get, request.path_params["lid"])
    except ValueError as exc:
        return _library_error(exc)
    return _landmark_json(rec)


async def edit_landmark(request: Request) -> Response:
    body = await _json(request)
    try:
        rec = await _in_thread(lambda: library.update(request.path_params["lid"], name=body.get("name"), link=body.get("link")))
    except ValueError as exc:
        return _library_error(exc)
    return _landmark_json(rec)


async def delete_landmark(request: Request) -> Response:
    """Moves the landmark folder to <library>/_trash/ — nothing is erased."""
    try:
        dest = await _in_thread(library.trash, request.path_params["lid"])
    except ValueError as exc:
        return _library_error(exc)
    return JSONResponse({"ok": True, "moved_to": str(dest)})


async def upload_landmark_version(request: Request) -> Response:
    """Stream a .glb into the landmark folder and keep it as the next version."""
    lid = request.path_params["lid"]
    raw_name = Path(request.query_params.get("name", "")).name
    if Path(raw_name).suffix.lower() != ".glb":
        return _error("Chỉ nhận file .glb (glTF binary).")
    try:
        folder = library.folder(lid)
    except ValueError as exc:
        return _library_error(exc)
    tmp = folder / f".upload-{secrets.token_hex(4)}.glb"
    try:
        size = 0
        with tmp.open("wb") as fh:
            async for chunk in request.stream():
                size += len(chunk)
                if size > MAX_GLB_BYTES:
                    return _error(f"File lớn hơn {MAX_GLB_BYTES >> 20} MB.")
                fh.write(chunk)
        rec = await _in_thread(lambda: library.add_version(lid, tmp, original_name=raw_name,
                                                           note=request.query_params.get("note", "")))
    except ValueError as exc:  # LibraryError, GlbError
        return _library_error(exc)
    except OSError as exc:
        return _error(f"Không ghi được file GLB: {exc}", 500)
    finally:
        tmp.unlink(missing_ok=True)
    log.info("Landmark %s: new version from %s (%d bytes)", lid, raw_name, size)
    return _landmark_json(rec, 201)


async def landmark_glb(request: Request) -> Response:
    try:
        path, version = await _in_thread(library.glb_path, request.path_params["lid"], request.path_params["n"])
    except ValueError as exc:
        return _library_error(exc)
    if request.query_params.get("download"):
        return FileResponse(path, filename=version["original_name"], media_type="model/gltf-binary")
    # Versions never change once written: the browser may keep them.
    return FileResponse(path, media_type="model/gltf-binary", headers={"Cache-Control": "max-age=31536000, immutable"})


async def landmark_shadow_file(request: Request) -> Response:
    """The ground shadow one version casts under a preset with sun (?preset=, default day), as
    GeoJSON: computed from the GLB on first use (app/landmark_shadow.py)."""
    preset = request.query_params.get("preset") or vmap.DEFAULT_PRESET
    if preset not in vmap.PRESETS:
        return _error(f"Không có preset {preset}.")
    if vmap.PRESETS[preset].shadow is None:
        return _error("Chế độ này không có nắng nên không đổ bóng.", 404)
    lid, n = request.path_params["lid"], request.path_params["n"]

    def cast() -> Path:
        rec = library._read(lid)
        return landmark_shadow.for_version(library.folder(lid), rec, library._version(rec, n), preset)

    try:
        path = await _in_thread(cast)
    except (landmark_library.LibraryError, glb.GlbError, shadows.ShadowError) as exc:
        return _library_error(exc)
    except tools.ToolMissingError as exc:
        return _error(str(exc), 500)
    return FileResponse(path, media_type="application/geo+json", headers=NO_CACHE)


async def set_landmark_placement(request: Request) -> Response:
    body = await _json(request)
    try:
        rec = await _in_thread(library.set_placement, request.path_params["lid"], request.path_params["n"], body)
    except ValueError as exc:
        return _library_error(exc)
    return _landmark_json(rec)


async def use_landmark_version(request: Request) -> Response:
    body = await _json(request)
    version = body.get("version")
    if version is not None and (isinstance(version, bool) or not isinstance(version, int)):
        return _error("Sai số version.")
    try:
        rec = await _in_thread(library.use, request.path_params["lid"], version, str(body.get("note") or ""))
    except ValueError as exc:
        return _library_error(exc)
    return _landmark_json(rec)


async def landmarks_manifest(request: Request) -> Response:
    """The version in use of every landmark with its placement and its ground shadow per preset
    with sun (?download=1 saves it)."""
    data = await _in_thread(lambda: landmark_shadow.add_to_manifest(library, library.manifest()))
    headers = dict(NO_CACHE)
    if request.query_params.get("download"):
        headers["Content-Disposition"] = 'attachment; filename="landmarks-manifest.json"'
    return JSONResponse(data, headers=headers)


def open_in_file_manager(path: Path) -> None:
    """Finder on macOS, Explorer on Windows, xdg-open elsewhere."""
    if sys.platform == "darwin":
        cmd = ["open", "-R", str(path)] if path.is_file() else ["open", str(path)]
    elif os.name == "nt":
        cmd = ["explorer", f"/select,{path}"] if path.is_file() else ["explorer", str(path)]
    else:
        cmd = ["xdg-open", str(path.parent if path.is_file() else path)]
    subprocess.Popen(cmd)  # noqa: S603 — fixed argv, no shell


async def _json(request: Request) -> dict[str, Any]:
    try:
        data = await request.json()
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


async def _in_thread(fn, *args):
    import anyio.to_thread

    return await anyio.to_thread.run_sync(lambda: fn(*args))


routes = [
    Route("/", index),
    Route("/preview", preview_page),
    Route("/api/health", health),
    Route("/api/upload", upload, methods=["PUT", "POST"]),
    Route("/api/probe", probe_file, methods=["POST"]),
    Route("/api/jobs", list_jobs, methods=["GET"]),
    Route("/api/jobs", create_job, methods=["POST"]),
    Route("/api/jobs/{job_id}", get_job),
    Route("/api/jobs/{job_id}/cancel", cancel_job, methods=["POST"]),
    Route("/api/files/{token}", get_file, methods=["GET", "HEAD"]),
    Route("/api/reveal", reveal, methods=["POST"]),
    Route("/api/base", get_base, methods=["GET"]),
    Route("/api/base/build", build_base, methods=["POST"]),
    Route("/api/vmap/presets", vmap_presets),
    Route("/api/vmap/style.json", vmap_style),
    Route("/api/vmap/assets/{path:path}", vmap_asset),
    Route("/api/shadows", building_shadows),
    Route("/api/building", building_footprint),
    Route("/api/landmarks", list_landmarks, methods=["GET"]),
    Route("/api/landmarks", register_landmark, methods=["POST"]),
    Route("/api/landmarks/manifest.json", landmarks_manifest),
    Route("/api/landmarks/render", render_landmarks, methods=["GET"]),
    Route("/api/landmarks/{lid}", get_landmark, methods=["GET"]),
    Route("/api/landmarks/{lid}", edit_landmark, methods=["PATCH"]),
    Route("/api/landmarks/{lid}", delete_landmark, methods=["DELETE"]),
    Route("/api/landmarks/{lid}/versions", upload_landmark_version, methods=["PUT", "POST"]),
    Route("/api/landmarks/{lid}/versions/{n:int}/glb", landmark_glb, methods=["GET", "HEAD"]),
    Route("/api/landmarks/{lid}/versions/{n:int}/shadow", landmark_shadow_file, methods=["GET"]),
    Route("/api/landmarks/{lid}/versions/{n:int}/placement", set_landmark_placement, methods=["PUT"]),
    Route("/api/landmarks/{lid}/use", use_landmark_version, methods=["POST"]),
    Mount("/static", RevalidatingStaticFiles(directory=STATIC_DIR), name="static"),
]

app = Starlette(routes=routes, middleware=[Middleware(LocalOnlyMiddleware)])


def main() -> None:
    parser = argparse.ArgumentParser(description="Building Data Studio — local web app")
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", DEFAULT_PORT)))
    parser.add_argument("--no-browser", action="store_true", help="Không tự mở trình duyệt")
    parser.add_argument("--no-base", action="store_true",
                        help="Không tự tạo base building OSM Hà Nội khi chưa có (hoặc BDS_BASE_AUTOBUILD=0)")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    missing = [n for n in tools.REQUIRED_TOOLS if tools.which(n) is None]
    if missing:
        log.warning("Thiếu công cụ: %s — cài bằng: brew install gdal tippecanoe pmtiles", ", ".join(missing))
    elif not args.no_base and os.environ.get("BDS_BASE_AUTOBUILD", "1") != "0" and not base_status()["ready"]:
        # First run: build the Hà Nội base in the background, so the 3D pages have buildings to show.
        start_base_build()
        print(f"  Chưa có base Hà Nội: đang tạo từ OpenStreetMap (Overture Maps), khoảng 3–5 phút → {BASE_DIR}", flush=True)

    url = f"http://{HOST}:{args.port}/"
    if not args.no_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    print(f"\n  Building Data Studio đang chạy tại {url}  (Ctrl+C để dừng)\n", flush=True)
    uvicorn.run(app, host=HOST, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
