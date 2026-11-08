"""Durable job store.

The queue lives in SQLite, not in memory. That is the whole point of the
offline path: a job accepted while the device has no network must still exist
after a browser refresh, a process restart, or a laptop lid close.

Timestamps are ISO-8601 UTC strings so the SQLite file stays readable with any
tool, and so ordering is lexicographic.
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

# status values
PENDING = "pending"
RUNNING = "running"
DONE = "done"
FAILED = "failed"
STATUSES = (PENDING, RUNNING, DONE, FAILED)

# sync states
SYNC_NOT_NEEDED = "not_needed"
SYNC_PENDING = "pending"
SYNC_SENT = "sent"
SYNC_FAILED = "failed"


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id           TEXT PRIMARY KEY,
    kind         TEXT NOT NULL,
    payload      TEXT NOT NULL,
    status       TEXT NOT NULL DEFAULT 'pending',
    mode         TEXT,
    result       TEXT,
    error        TEXT,
    attempts     INTEGER NOT NULL DEFAULT 0,
    sync_state   TEXT NOT NULL DEFAULT 'not_needed',
    sync_note    TEXT,
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL,
    started_at   TEXT,
    finished_at  TEXT
);
CREATE INDEX IF NOT EXISTS idx_jobs_status_created ON jobs (status, created_at);
CREATE INDEX IF NOT EXISTS idx_jobs_sync_state ON jobs (sync_state, updated_at);
"""


@dataclass(slots=True)
class Job:
    id: str
    kind: str
    payload: dict[str, Any]
    status: str
    mode: str | None
    result: dict[str, Any] | None
    error: str | None
    attempts: int
    sync_state: str
    sync_note: str | None
    created_at: str
    updated_at: str
    started_at: str | None
    finished_at: str | None

    @classmethod
    def from_row(cls, row: sqlite3.Row) -> "Job":
        def _load(value: str | None):
            if value is None:
                return None
            try:
                return json.loads(value)
            except json.JSONDecodeError:
                return {"_raw": value}

        return cls(
            id=row["id"],
            kind=row["kind"],
            payload=_load(row["payload"]) or {},
            status=row["status"],
            mode=row["mode"],
            result=_load(row["result"]),
            error=row["error"],
            attempts=row["attempts"],
            sync_state=row["sync_state"],
            sync_note=row["sync_note"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            started_at=row["started_at"],
            finished_at=row["finished_at"],
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "kind": self.kind,
            "payload": self.payload,
            "status": self.status,
            "mode": self.mode,
            "result": self.result,
            "error": self.error,
            "attempts": self.attempts,
            "sync_state": self.sync_state,
            "sync_note": self.sync_note,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }

    @property
    def is_terminal(self) -> bool:
        return self.status in (DONE, FAILED)


class JobStore:
    """Single-writer SQLite store.

    One connection guarded by a re-entrant lock. Writes are tiny and local, and
    async callers go through ``asyncio.to_thread`` so the event loop is never
    blocked by disk I/O.
    """

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._conn.executescript(SCHEMA)
        self._conn.commit()

    # ------------------------------------------------------------------ sync internals
    def _insert(self, kind: str, payload: dict[str, Any], sync_state: str) -> Job:
        now = utcnow()
        job_id = uuid.uuid4().hex[:16]
        with self._lock:
            self._conn.execute(
                "INSERT INTO jobs (id, kind, payload, status, attempts, sync_state,"
                " created_at, updated_at) VALUES (?, ?, ?, ?, 0, ?, ?, ?)",
                (job_id, kind, json.dumps(payload, ensure_ascii=False), PENDING, sync_state, now, now),
            )
            self._conn.commit()
            return self._fetch(job_id)

    def _get(self, job_id: str) -> Job | None:
        with self._lock:
            return self._fetch(job_id)

    def _fetch(self, job_id: str) -> Job | None:
        row = self._conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        return Job.from_row(row) if row else None

    def _list(self, limit: int, status: str | None, newest_first: bool = True) -> list[Job]:
        order = "DESC" if newest_first else "ASC"
        with self._lock:
            if status:
                rows = self._conn.execute(
                    f"SELECT * FROM jobs WHERE status = ? ORDER BY created_at {order} LIMIT ?",
                    (status, limit),
                ).fetchall()
            else:
                rows = self._conn.execute(
                    f"SELECT * FROM jobs ORDER BY created_at {order} LIMIT ?", (limit,)
                ).fetchall()
        return [Job.from_row(row) for row in rows]

    def _claim_next(self) -> Job | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM jobs WHERE status = ? ORDER BY created_at ASC LIMIT 1",
                (PENDING,),
            ).fetchone()
            if row is None:
                return None
            now = utcnow()
            claimed = self._conn.execute(
                "UPDATE jobs SET status=?, attempts=attempts+1, started_at=?, updated_at=? "
                "WHERE id=? AND status=?",
                (RUNNING, now, now, row["id"], PENDING),
            )
            self._conn.commit()
            if claimed.rowcount != 1:  # lost a race with another writer
                return None
            return Job.from_row(self._conn.execute(
                "SELECT * FROM jobs WHERE id=?", (row["id"],)
            ).fetchone())

    def _finish(
        self,
        job_id: str,
        result: dict[str, Any],
        mode: str,
        sync_state: str,
        sync_note: str | None = None,
    ) -> Job | None:
        now = utcnow()
        with self._lock:
            self._conn.execute(
                "UPDATE jobs SET status=?, result=?, error=NULL, mode=?, sync_state=?, "
                "sync_note=?, finished_at=?, updated_at=? WHERE id=?",
                (
                    DONE,
                    json.dumps(result, ensure_ascii=False),
                    mode,
                    sync_state,
                    sync_note,
                    now,
                    now,
                    job_id,
                ),
            )
            self._conn.commit()
            return self._fetch(job_id)

    def _fail(self, job_id: str, error: str, retry: bool) -> Job | None:
        now = utcnow()
        status = PENDING if retry else FAILED
        with self._lock:
            self._conn.execute(
                "UPDATE jobs SET status=?, error=?, started_at=NULL, updated_at=? "
                "WHERE id=?",
                (status, error[:2000], now, job_id),
            )
            if not retry:
                self._conn.execute(
                    "UPDATE jobs SET finished_at=? WHERE id=?", (now, job_id)
                )
            self._conn.commit()
            return self._fetch(job_id)

    def _requeue_stale(self) -> int:
        """Return jobs orphaned by a crash to the queue."""
        with self._lock:
            cur = self._conn.execute(
                "UPDATE jobs SET status=?, started_at=NULL, updated_at=? WHERE status=?",
                (PENDING, utcnow(), RUNNING),
            )
            self._conn.commit()
            return cur.rowcount

    def _stats(self) -> dict[str, int]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT status, COUNT(*) AS n FROM jobs GROUP BY status"
            ).fetchall()
            by_status = {row["status"]: row["n"] for row in rows}
            pending_sync = self._conn.execute(
                "SELECT COUNT(*) AS n FROM jobs WHERE sync_state=?", (SYNC_PENDING,)
            ).fetchone()["n"]
        total = sum(by_status.values())
        return {
            "total": total,
            "pending": by_status.get(PENDING, 0),
            "running": by_status.get(RUNNING, 0),
            "done": by_status.get(DONE, 0),
            "failed": by_status.get(FAILED, 0),
            "pending_sync": pending_sync,
        }

    def _pending_sync(self, limit: int) -> list[Job]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM jobs WHERE sync_state=? AND status=? "
                "ORDER BY created_at ASC LIMIT ?",
                (SYNC_PENDING, DONE, limit),
            ).fetchall()
        return [Job.from_row(row) for row in rows]

    def _set_sync_state(self, job_id: str, state: str, note: str | None = None) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE jobs SET sync_state=?, sync_note=?, updated_at=? WHERE id=?",
                (state, note, utcnow(), job_id),
            )
            self._conn.commit()

    # ------------------------------------------------------------------ async API
    async def create(
        self, kind: str, payload: dict[str, Any], sync_state: str = SYNC_NOT_NEEDED
    ) -> Job:
        return await asyncio.to_thread(self._insert, kind, payload, sync_state)

    async def get(self, job_id: str) -> Job | None:
        return await asyncio.to_thread(self._get, job_id)

    async def list(
        self, limit: int = 25, status: str | None = None, newest_first: bool = True
    ) -> list[Job]:
        return await asyncio.to_thread(self._list, limit, status, newest_first)

    async def claim_next(self) -> Job | None:
        return await asyncio.to_thread(self._claim_next)

    async def finish(
        self,
        job_id: str,
        result: dict[str, Any],
        mode: str,
        sync_state: str = SYNC_NOT_NEEDED,
        sync_note: str | None = None,
    ) -> Job | None:
        return await asyncio.to_thread(self._finish, job_id, result, mode, sync_state, sync_note)

    async def fail(self, job_id: str, error: str, retry: bool = False) -> Job | None:
        return await asyncio.to_thread(self._fail, job_id, error, retry)

    async def requeue_stale(self) -> int:
        return await asyncio.to_thread(self._requeue_stale)

    async def stats(self) -> dict[str, int]:
        return await asyncio.to_thread(self._stats)

    async def pending_sync(self, limit: int = 20) -> list[Job]:
        return await asyncio.to_thread(self._pending_sync, limit)

    async def set_sync_state(self, job_id: str, state: str, note: str | None = None) -> None:
        await asyncio.to_thread(self._set_sync_state, job_id, state, note)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def __enter__(self) -> "JobStore":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

