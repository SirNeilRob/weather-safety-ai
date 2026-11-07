"""Background job workers.

Workers are decoupled from HTTP requests on purpose. ``POST /v1/jobs`` returns
in a couple of milliseconds, and the actual processing happens here. That means
a closed browser tab, a slow local model, or a 60 second Swarm timeout never
blocks the UI thread.
"""

from __future__ import annotations

import asyncio
import logging

logger = logging.getLogger(__name__)


class WorkerPool:
    def __init__(
        self,
        store,
        router,
        concurrency: int = 2,
        max_attempts: int = 3,
        job_timeout: float = 60.0,
        idle_poll: float = 1.0,
    ) -> None:
        self.store = store
        self.router = router
        self.concurrency = max(1, int(concurrency))
        self.max_attempts = max(1, int(max_attempts))
        self.job_timeout = float(job_timeout)
        self.idle_poll = max(0.05, float(idle_poll))

        self._tasks: list[asyncio.Task[None]] = []
        self._stop = asyncio.Event()
        self._wake = asyncio.Event()
        self.processed = 0
        self.failed = 0

    # ------------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        if self._tasks:
            return
        self._stop.clear()
        for index in range(self.concurrency):
            self._tasks.append(
                asyncio.create_task(self._loop(index), name=f"job-worker-{index}")
            )
        logger.info("started %d job worker(s)", self.concurrency)

    async def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        for task in self._tasks:
            task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        self._tasks.clear()

    def wake(self) -> None:
        """Tell idle workers that new work arrived."""
        self._wake.set()

    # ------------------------------------------------------------------ loop
    async def _loop(self, index: int) -> None:
        while not self._stop.is_set():
            try:
                job = await self.store.claim_next()
            except Exception:  # pragma: no cover - defensive
                logger.exception("worker %d could not claim a job", index)
                await self._idle()
                continue

            if job is None:
                await self._idle()
                continue

            await self._handle(job)

    async def _idle(self) -> None:
        try:
            await asyncio.wait_for(self._wake.wait(), timeout=self.idle_poll)
        except (asyncio.TimeoutError, TimeoutError):
            pass
        self._wake.clear()

    async def _handle(self, job) -> None:
        prefer_local = bool(job.payload.get("_prefer_local"))
        payload = {
            key: value for key, value in job.payload.items() if key != "_prefer_local"
        }
        try:
            outcome = await asyncio.wait_for(
                self.router.process(job.kind, payload, prefer_local=prefer_local),
                timeout=self.job_timeout,
            )
        except (asyncio.TimeoutError, TimeoutError):
            await self._retry_or_fail(job, f"job timed out after {self.job_timeout:.0f}s")
            return
        except Exception as exc:
            logger.exception("job %s crashed", job.id)
            await self._retry_or_fail(job, f"{type(exc).__name__}: {exc}")
            return

        await self.store.finish(
            job.id,
            result=outcome.result,
            mode=outcome.mode,
            sync_state=outcome.sync_state,
            sync_note=outcome.note,
        )
        self.processed += 1
        logger.info("job %s done via %s", job.id, outcome.mode)

    async def _retry_or_fail(self, job, error: str) -> None:
        retry = job.attempts < self.max_attempts
        updated = await self.store.fail(job.id, error, retry=retry)
        if not retry:
            self.failed += 1
        logger.warning(
            "job %s %s: %s", job.id, "requeued" if retry else "failed", error
        )
        if updated is not None and retry:
            # Small backoff so a crashing processor cannot spin the queue.
            await asyncio.sleep(min(2.0, 0.25 * max(1, job.attempts)))
            self.wake()
