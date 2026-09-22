"""SQLite setup. One table: jobs. Everything else lives in JSON files."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from .jobs import new_job_id

# `seq` orders the table; `id` is the random string the UI and the URLs use.
SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    seq          INTEGER PRIMARY KEY AUTOINCREMENT,
    id           TEXT    NOT NULL UNIQUE,
    component_id TEXT    NOT NULL,
    action       TEXT    NOT NULL,
    status       TEXT    NOT NULL DEFAULT 'queued',
    log          TEXT    NOT NULL DEFAULT '',
    error        TEXT,
    created_at   TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%SZ', 'now')),
    started_at   TEXT,
    finished_at  TEXT
);

CREATE INDEX IF NOT EXISTS jobs_by_recency ON jobs (seq DESC);
"""

COLUMNS = (
    "component_id",
    "action",
    "status",
    "log",
    "error",
    "created_at",
    "started_at",
    "finished_at",
)


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    _migrate_counter_ids(conn)
    conn.executescript(SCHEMA)
    conn.commit()
    return conn


def _migrate_counter_ids(conn: sqlite3.Connection) -> None:
    """Rebuild a pre-0.3 jobs table, whose id was an autoincrementing integer.

    History is worth keeping — the rows carry the log of every install this
    machine has done — so each old row gets a random id in place of its number.
    """
    columns = {row["name"] for row in conn.execute("PRAGMA table_info(jobs)")}
    if not columns or "seq" in columns:
        return
    old = conn.execute(f"SELECT {', '.join(COLUMNS)} FROM jobs ORDER BY id ASC").fetchall()
    conn.execute("DROP TABLE jobs")
    conn.executescript(SCHEMA)
    placeholders = ", ".join("?" * (len(COLUMNS) + 1))
    conn.executemany(
        f"INSERT INTO jobs (id, {', '.join(COLUMNS)}) VALUES ({placeholders})",
        [(new_job_id(), *tuple(row)) for row in old],
    )
    conn.commit()
