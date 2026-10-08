"""Locate and run the external CLI tools (GDAL, tippecanoe, pmtiles).

All heavy lifting is delegated to these tools: they stream data from disk, so
memory stays flat regardless of input size.
"""

from __future__ import annotations

import functools
import logging
import os
import re
import shutil
import signal
import subprocess
import threading
from collections.abc import Callable
from pathlib import Path

log = logging.getLogger(__name__)

REQUIRED_TOOLS = ("ogr2ogr", "ogrinfo", "tippecanoe")
OPTIONAL_TOOLS = ("pmtiles",)

# Apps launched outside a login shell often miss Homebrew on PATH.
EXTRA_BIN_DIRS = (Path("/opt/homebrew/bin"), Path("/usr/local/bin"))

KILL_GRACE_SECONDS = 5
READ_CHUNK_BYTES = 65536

_PERCENT_RE = re.compile(r"(\d{1,3}(?:\.\d+)?)%")
_GDAL_PROGRESS_RE = re.compile(r"(\d{1,3})(?:\.\.\.| - done)")


class ToolMissingError(RuntimeError):
    """Raised when a required external command cannot be found."""


class CancelledError(RuntimeError):
    """Raised when a running command was cancelled by the user."""


def which(name: str) -> str | None:
    """Return the absolute path of an executable, also checking Homebrew dirs."""
    found = shutil.which(name)
    if found:
        return found
    for directory in EXTRA_BIN_DIRS:
        candidate = directory / name
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return str(candidate)
    return None


def require(name: str) -> str:
    """Return the path of a required tool or raise ToolMissingError."""
    path = which(name)
    if path is None:
        raise ToolMissingError(
            f"Không tìm thấy lệnh '{name}'. Cài bằng: brew install gdal tippecanoe pmtiles"
        )
    return path


@functools.cache
def tool_versions() -> dict[str, dict[str, str | None]]:
    """Return {tool: {"path": ..., "version": ...}} for every known tool."""
    version_args = {
        "ogr2ogr": ["--version"],
        "ogrinfo": ["--version"],
        "tippecanoe": ["--version"],
        "pmtiles": ["version"],
    }
    result: dict[str, dict[str, str | None]] = {}
    for name in (*REQUIRED_TOOLS, *OPTIONAL_TOOLS):
        path = which(name)
        version = None
        if path:
            try:
                proc = subprocess.run(
                    [path, *version_args[name]],
                    capture_output=True,
                    text=True,
                    timeout=10,
                    check=False,
                )
                text = (proc.stdout or proc.stderr).strip().splitlines()
                version = text[0] if text else None
            except (OSError, subprocess.TimeoutExpired) as exc:
                log.warning("Could not read version of %s: %s", name, exc)
        result[name] = {"path": path, "version": version}
    return result


def parse_progress(text: str) -> float | None:
    """Extract a 0-100 progress value from tippecanoe or GDAL output.

    Args:
        text: A line or partial line of tool output.

    Returns:
        The last progress percentage found, or None.
    """
    matches = _PERCENT_RE.findall(text)
    if matches:
        return min(100.0, float(matches[-1]))
    matches = _GDAL_PROGRESS_RE.findall(text)
    if matches:
        return min(100.0, float(matches[-1]))
    return None


def run_capture(cmd: list[str], timeout: float = 300) -> subprocess.CompletedProcess[str]:
    """Run a short command and capture its output (no streaming)."""
    log.debug("run: %s", cmd)
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)


class ProcessRunner:
    """Run one command at a time with streamed output and cancellation."""

    def __init__(self) -> None:
        self._proc: subprocess.Popen[bytes] | None = None
        self._lock = threading.Lock()
        self._cancel_hooks: list[Callable[[], None]] = []
        self.cancelled = False

    def add_cancel_hook(self, hook: Callable[[], None]) -> None:
        """Call hook on cancel() too, e.g. to interrupt an in-process DuckDB query."""
        with self._lock:
            self._cancel_hooks.append(hook)
        if self.cancelled:
            hook()

    def run(
        self,
        cmd: list[str],
        on_line: Callable[[str], None],
        on_progress: Callable[[float], None] | None = None,
        cwd: Path | None = None,
    ) -> int:
        """Run cmd, calling on_line per output line and on_progress on % updates.

        Tools redraw progress with carriage returns, so output is split on both
        \\r and \\n. Lines that are pure progress go to on_progress only.

        Returns:
            The process exit code.

        Raises:
            CancelledError: If cancel() was called before or during the run.
        """
        if self.cancelled:
            raise CancelledError("Đã huỷ")
        popen_kwargs: dict = {}
        if os.name == "posix":
            popen_kwargs["start_new_session"] = True
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            cwd=cwd,
            **popen_kwargs,
        )
        with self._lock:
            self._proc = proc
        assert proc.stdout is not None
        buffer = ""
        try:
            while True:
                chunk = proc.stdout.read1(READ_CHUNK_BYTES)
                if not chunk:
                    break
                buffer += chunk.decode("utf-8", errors="replace")
                parts = re.split(r"[\r\n]", buffer)
                buffer = parts.pop()
                for part in parts:
                    self._emit(part, on_line, on_progress)
                if buffer and on_progress:
                    value = parse_progress(buffer)
                    if value is not None:
                        on_progress(value)
            if buffer:
                self._emit(buffer, on_line, on_progress)
            code = proc.wait()
        finally:
            with self._lock:
                self._proc = None
        if self.cancelled:
            raise CancelledError("Đã huỷ")
        return code

    @staticmethod
    def _emit(
        line: str,
        on_line: Callable[[str], None],
        on_progress: Callable[[float], None] | None,
    ) -> None:
        stripped = line.strip()
        if not stripped:
            return
        value = parse_progress(stripped)
        if value is not None and on_progress:
            on_progress(value)
            # Pure progress redraws ("  42.1%  14/1234/567") would flood the log.
            if _is_progress_only(stripped):
                return
        on_line(stripped)

    def cancel(self) -> None:
        """Stop the running command (and its children) and block further runs."""
        self.cancelled = True
        with self._lock:
            proc = self._proc
            hooks = list(self._cancel_hooks)
        for hook in hooks:
            try:
                hook()
            except Exception:  # noqa: BLE001 — a failing hook must not block killing the process
                log.exception("Cancel hook failed")
        if proc is None or proc.poll() is not None:
            return
        try:
            if os.name == "posix":
                os.killpg(proc.pid, signal.SIGTERM)
            else:
                proc.terminate()
            proc.wait(timeout=KILL_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            if os.name == "posix":
                os.killpg(proc.pid, signal.SIGKILL)
            else:
                proc.kill()
        except ProcessLookupError:
            pass


_PROGRESS_ONLY_RE = re.compile(
    r"^(?:[\w ]+:\s*)?\d{1,3}(?:\.\d+)?%(?:\s+\d+/\d+/\d+)?$|^(?:\d{1,3}\.\.\.)+\d*(?: - done\.?)?$"
)


def _is_progress_only(line: str) -> bool:
    return bool(_PROGRESS_ONLY_RE.match(line))
