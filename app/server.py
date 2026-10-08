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

import uvicorn
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, Response
from starlette.routing import Mount, Route
from starlette.staticfiles import StaticFiles

from . import __version__, pipeline, probe, tools
from .jobs import FileRegistry, JobManager

log = logging.getLogger(__name__)

APP_DIR = Path(__file__).resolve().parent
STATIC_DIR = APP_DIR / "static"
UPLOAD_DIR = APP_DIR.parent / "uploads"
DEFAULT_PORT = 8765
HOST = "127.0.0.1"
ALLOWED_HOSTS = {"127.0.0.1", "localhost"}

registry = FileRegistry()
jobs = JobManager(execute=lambda job: pipeline.execute(job, registry))


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
    try:
        cfg = pipeline.parse_config(body)
    except pipeline.ConfigError as exc:
        return _error(str(exc))
    job = jobs.submit(body, name=cfg.out_name)
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
    Mount("/static", RevalidatingStaticFiles(directory=STATIC_DIR), name="static"),
]

app = Starlette(routes=routes, middleware=[Middleware(LocalOnlyMiddleware)])


def main() -> None:
    parser = argparse.ArgumentParser(description="Building Data Studio — local web app")
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", DEFAULT_PORT)))
    parser.add_argument("--no-browser", action="store_true", help="Không tự mở trình duyệt")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    missing = [n for n in tools.REQUIRED_TOOLS if tools.which(n) is None]
    if missing:
        log.warning("Thiếu công cụ: %s — cài bằng: brew install gdal tippecanoe pmtiles", ", ".join(missing))

    url = f"http://{HOST}:{args.port}/"
    if not args.no_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    print(f"\n  Building Data Studio đang chạy tại {url}  (Ctrl+C để dừng)\n", flush=True)
    uvicorn.run(app, host=HOST, port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
