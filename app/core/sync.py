"""Reconciliation of offline work.

Jobs processed by the local engine are marked ``sync_state=pending``. This
worker pushes them to ``SWARM_SYNC_ENDPOINT`` once the link is back, so the
remote side eventually sees the work that happened during the outage.

If no sync endpoint is configured the jobs stay pending and the health endpoint
reports the backlog. That is deliberate: silently marking them "synced" would
be a lie the demo cannot afford.
"""

from __future__ import annotations

import asyncio
import logging

from .db import SYNC_FAILED, SYNC_PENDING, SYNC_SENT
from .processors.base import ProcessorUnavailable

logger = logging.getLogger(__name__)


def _is_permanent(error: str) -> bool:
    """4xx means the request itself is wrong; retrying forever is pointless."""
    return "HTTP 4" in error


class SyncWorker:
    def __init__(
        self,
        store,
        swarm,
        monitor,
        interval: float = 10.0,
        batch: int = 20,
        request_timeout: float = 20.0,
    ) -> None:
        self.store = store
        self.swarm = swarm
        self.monitor = monitor
        self.interval = max(0.5, float(interval))
        self.batch = max(1, int(batch))
        self.request_timeout = request_timeout

        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self.sent = 0
        self.failed = 0
        self.last_result: dict | None = None

    # ------------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._loop(), name="sync-worker")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None

    async def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.last_result = await self.run_once()
            except Exception:
                self.last_result = {"status": "error"}
            try:
                await asyncio.sleep(self.interval)
            except asyncio.CancelledError:
                return

    # ------------------------------------------------------------------ work
    async def run_once(self) -> dict:
        if not getattr(self.swarm, "can_sync", False):
            return {
                "status": "disabled",
                "reason": "SWARM_SYNC_ENDPOINT is not configured",
            }

        if not self.monitor.is_online():
            return {"status": "skipped", "reason": "device offline"}

        jobs = await self.store.pending_sync(self.batch)
        if not jobs:
            return {"status": "idle", "sent": 0, "remaining": 0}

        sent = 0
        for job in jobs:
            record = {
                "job_id": job.id,
                "kind": job.kind,
                "mode": job.mode,
                "created_at": job.created_at,
                "finished_at": job.finished_at,
                "result": job.result,
            }
            try:
                await self.swarm.push_sync(record)
            except ProcessorUnavailable as exc:
                error = str(exc)
                if _is_permanent(error):
                    await self.store.set_sync_state(job.id, SYNC_FAILED, error)
                    self.failed += 1
                else:
                    # Transient: leave it pending so the next pass retries.
                    await self.store.set_sync_state(job.id, SYNC_PENDING, error)
                logger.warning("sync of job %s failed: %s", job.id, error)
                break
            except Exception as exc:  # pragma: no cover - defensive
                await self.store.set_sync_state(
                    job.id, SYNC_PENDING, f"{type(exc).__name__}: {exc}"
                )
                logger.exception("unexpected sync failure for job %s", job.id)
                break
            else:
                await self.store.set_sync_state(job.id, SYNC_SENT, None)
                sent += 1

        self.sent += sent
        stats = await self.store.stats()
        return {
            "status": "ok",
            "sent": sent,
            "remaining": stats.get("pending_sync", 0),
        }
