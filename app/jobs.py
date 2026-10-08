"""In-memory job queue: one worker thread runs conversions one at a time."""

from __future__ import annotations

import logging
import queue
import secrets
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .tools import ProcessRunner

log = logging.getLogger(__name__)

MAX_LOG_LINES = 20000
TOKEN_BYTES = 12


class FileRegistry:
    """Maps opaque tokens to local files so the browser never sends raw paths."""

    def __init__(self) -> None:
        self._paths: dict[str, Path] = {}
        self._by_path: dict[Path, str] = {}
        self._lock = threading.Lock()

    def register(self, path: Path) -> str:
        """Return a stable token for path (same path -> same token)."""
        resolved = path.resolve()
        with self._lock:
            token = self._by_path.get(resolved)
            if token is None:
                token = secrets.token_urlsafe(TOKEN_BYTES)
                self._paths[token] = resolved
                self._by_path[resolved] = token
            return token

    def get(self, token: str) -> Path | None:
        with self._lock:
            return self._paths.get(token)


@dataclass
class Step:
    """One pipeline stage as shown in the UI."""

    key: str
    title: str
    status: str = "pending"  # pending | running | done | skipped | failed
    progress: float | None = None
    started: float | None = None
    ended: float | None = None
    detail: str | None = None


@dataclass
class Job:
    """A single conversion run and everything the UI needs to display it."""

    id: str
    config: dict[str, Any]
    name: str
    created: float = field(default_factory=time.time)
    status: str = "queued"  # queued | running | done | failed | cancelled
    steps: list[Step] = field(default_factory=list)
    log: list[str] = field(default_factory=list)
    outputs: dict[str, dict[str, Any]] = field(default_factory=dict)
    report: dict[str, Any] | None = None
    error: str | None = None
    output_dir: str | None = None
    output_dir_token: str | None = None
    started: float | None = None
    ended: float | None = None
    runner: ProcessRunner = field(default_factory=ProcessRunner)
    log_file: Any = None
    lock: threading.RLock = field(default_factory=threading.RLock)

    def add_log(self, line: str) -> None:
        stamp = time.strftime("%H:%M:%S")
        entry = f"[{stamp}] {line}"
        with self.lock:
            if len(self.log) < MAX_LOG_LINES:
                self.log.append(entry)
            elif len(self.log) == MAX_LOG_LINES:
                self.log.append("[log trên giao diện bị cắt — xem đầy đủ trong log.txt]")
            if self.log_file is not None:
                self.log_file.write(entry + "\n")
                self.log_file.flush()

    def step(self, key: str) -> Step:
        for s in self.steps:
            if s.key == key:
                return s
        raise KeyError(key)

    def to_dict(self, log_since: int = 0) -> dict[str, Any]:
        with self.lock:
            return {
                "id": self.id,
                "name": self.name,
                "status": self.status,
                "created": self.created,
                "started": self.started,
                "ended": self.ended,
                "error": self.error,
                "output_dir": self.output_dir,
                "output_dir_token": self.output_dir_token,
                "steps": [vars(s).copy() for s in self.steps],
                "outputs": {k: dict(v) for k, v in self.outputs.items()},
                "report": self.report,
                "log": self.log[log_since:],
                "log_total": len(self.log),
            }

    def summary(self) -> dict[str, Any]:
        with self.lock:
            return {
                "id": self.id,
                "name": self.name,
                "status": self.status,
                "created": self.created,
                "ended": self.ended,
                "outputs": list(self.outputs),
            }


class JobManager:
    """Owns all jobs and the single worker thread that executes them."""

    def __init__(self, execute: Callable[[Job], None]) -> None:
        self._execute = execute
        self._jobs: dict[str, Job] = {}
        self._queue: queue.Queue[Job] = queue.Queue()
        self._lock = threading.Lock()
        self._worker = threading.Thread(target=self._loop, name="job-worker", daemon=True)
        self._worker.start()

    def submit(self, config: dict[str, Any], name: str) -> Job:
        job = Job(id=secrets.token_hex(6), config=config, name=name)
        with self._lock:
            self._jobs[job.id] = job
        self._queue.put(job)
        return job

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._jobs.get(job_id)

    def list(self) -> list[Job]:
        with self._lock:
            return sorted(self._jobs.values(), key=lambda j: j.created, reverse=True)

    def cancel(self, job_id: str) -> bool:
        job = self.get(job_id)
        if job is None or job.status in ("done", "failed", "cancelled"):
            return False
        job.runner.cancel()
        with job.lock:
            if job.status == "queued":
                job.status = "cancelled"
                job.ended = time.time()
        return True

    def _loop(self) -> None:
        while True:
            job = self._queue.get()
            if job.status == "cancelled":
                continue
            try:
                self._execute(job)
            except Exception:  # noqa: BLE001 — last-resort guard so the worker survives
                log.exception("Job %s crashed", job.id)
                with job.lock:
                    job.status = "failed"
                    job.error = job.error or "Lỗi không mong đợi — xem log."
                    job.ended = time.time()
