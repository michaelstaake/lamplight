"""Job records and the single-slot worker that runs them.

Lamplight runs one job at a time on purpose: two concurrent `apt-get install`
calls would just deadlock on the dpkg lock.

Job logs are buffered in memory and flushed to SQLite on a size or time
threshold. An `apt-get install` emits thousands of lines, and committing each
one separately made installs visibly slower than running apt by hand.

Job ids are random six-character strings rather than a counter, so "job #6"
never reads as "the sixth thing this machine ever did" and two panels cannot
show the same id for different work.
"""

from __future__ import annotations

import logging
import secrets
import sqlite3
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

QUEUED = "queued"
RUNNING = "running"
SUCCESS = "success"
FAILED = "failed"
TERMINAL_STATUSES = frozenset({SUCCESS, FAILED})

# No 0 and no o: the two characters people mistype when reading an id back off
# a screen. Everything else lowercase, so an id is never shift-typed.
ID_ALPHABET = "123456789abcdefghijklmnpqrstuvwxyz"
ID_LENGTH = 6
ID_ATTEMPTS = 8

FLUSH_BYTES = 4096
FLUSH_SECONDS = 0.5
DEFAULT_HISTORY = 200

log = logging.getLogger(__name__)


def utcnow() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def new_job_id() -> str:
    return "".join(secrets.choice(ID_ALPHABET) for _ in range(ID_LENGTH))


@dataclass
class _Pending:
    parts: list[str] = field(default_factory=list)
    size: int = 0
    flushed_at: float = 0.0


class JobStore:
    def __init__(self, conn: sqlite3.Connection, history: int = DEFAULT_HISTORY) -> None:
        self.conn = conn
        self.history = history
        self._lock = threading.Lock()
        self._pending: dict[str, _Pending] = {}

    def create(self, component_id: str, action: str) -> str:
        with self._lock:
            job_id = self._insert_locked(component_id, action)
        self._prune()
        return job_id

    def _insert_locked(self, component_id: str, action: str) -> str:
        """Insert with a fresh random id, retrying the vanishingly rare collision."""
        for _ in range(ID_ATTEMPTS):
            job_id = new_job_id()
            try:
                self.conn.execute(
                    "INSERT INTO jobs (id, component_id, action, status) VALUES (?, ?, ?, ?)",
                    (job_id, component_id, action, QUEUED),
                )
            except sqlite3.IntegrityError:
                continue
            self.conn.commit()
            return job_id
        raise RuntimeError(f"could not find a free job id in {ID_ATTEMPTS} attempts")

    def get(self, job_id: str) -> dict | None:
        with self._lock:
            row = self.conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
            if row is None:
                return None
            job = dict(row)
            pending = self._pending.get(job_id)
            if pending is not None:
                job["log"] = job["log"] + "".join(pending.parts)
            return job

    def list(self, limit: int = 50) -> list[dict]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT id, component_id, action, status, error,"
                " created_at, started_at, finished_at"
                " FROM jobs ORDER BY seq DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [dict(row) for row in rows]

    def list_for(self, component_id: str, limit: int = 10) -> list[dict]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT id, component_id, action, status, error,"
                " created_at, started_at, finished_at"
                " FROM jobs WHERE component_id = ? ORDER BY seq DESC LIMIT ?",
                (component_id, limit),
            ).fetchall()
        return [dict(row) for row in rows]

    def append_log(self, job_id: str, chunk: str) -> None:
        if not chunk:
            return
        with self._lock:
            pending = self._pending.setdefault(job_id, _Pending(flushed_at=time.monotonic()))
            pending.parts.append(chunk)
            pending.size += len(chunk)
            stale = time.monotonic() - pending.flushed_at >= FLUSH_SECONDS
            if pending.size >= FLUSH_BYTES or stale:
                self._flush_locked(job_id)

    def flush(self, job_id: str) -> None:
        with self._lock:
            self._flush_locked(job_id)

    def mark_running(self, job_id: str) -> None:
        with self._lock:
            self.conn.execute(
                "UPDATE jobs SET status = ?, started_at = ? WHERE id = ?",
                (RUNNING, utcnow(), job_id),
            )
            self.conn.commit()

    def finish(self, job_id: str, status: str, error: str | None = None) -> None:
        with self._lock:
            self._flush_locked(job_id)
            self._pending.pop(job_id, None)
            self.conn.execute(
                "UPDATE jobs SET status = ?, error = ?, finished_at = ? WHERE id = ?",
                (status, error, utcnow(), job_id),
            )
            self.conn.commit()

    def _flush_locked(self, job_id: str) -> None:
        pending = self._pending.get(job_id)
        if pending is None or not pending.parts:
            return
        self.conn.execute(
            "UPDATE jobs SET log = log || ? WHERE id = ?", ("".join(pending.parts), job_id)
        )
        self.conn.commit()
        pending.parts.clear()
        pending.size = 0
        pending.flushed_at = time.monotonic()

    def _prune(self) -> None:
        """Keep the newest `history` jobs so the log column cannot grow forever."""
        with self._lock:
            self.conn.execute(
                "DELETE FROM jobs WHERE seq NOT IN"
                " (SELECT seq FROM jobs ORDER BY seq DESC LIMIT ?)",
                (self.history,),
            )
            self.conn.commit()


class JobRunner:
    """One worker slot. submit() returns False when a job is already running."""

    def __init__(self) -> None:
        self._slot = threading.Lock()
        self.current_id: str | None = None
        self.last_error: str | None = None

    @property
    def busy(self) -> bool:
        return self._slot.locked()

    def submit(self, job_id: str, fn: Callable[[], None]) -> bool:
        if not self._slot.acquire(blocking=False):
            return False
        self.current_id = job_id

        def wrapper() -> None:
            try:
                fn()
            except Exception:  # noqa: BLE001 — a dead worker thread must not take the slot with it
                self.last_error = f"job {job_id} crashed"
                log.exception("job %s crashed outside its own error handling", job_id)
            finally:
                self.current_id = None
                self._slot.release()

        threading.Thread(target=wrapper, name=f"lamplight-job-{job_id}", daemon=True).start()
        return True
