"""Processor routing.

Decision order for every job:

1. ``prefer_local`` on the request, or device offline  -> local
2. Swarm circuit breaker open (repeated failures)       -> local
3. Swarm not configured                                 -> local
4. Otherwise try Swarm, and on *any* deliberate failure  -> local + mark for sync

Step 4 is what keeps the product usable: the user gets a result either way, and
the fact that Swarm was skipped is recorded on the job (``mode`` + ``note``) so
the UI can be honest about it instead of pretending the cloud answered.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Callable

from .db import SYNC_NOT_NEEDED, SYNC_PENDING
from .processors.base import ProcessorUnavailable

logger = logging.getLogger(__name__)

MODE_SWARM = "swarm"
MODE_LOCAL = "local"


@dataclass(slots=True)
class RouteOutcome:
    result: dict
    mode: str
    sync_state: str
    note: str | None = None


class SmartRouter:
    def __init__(
        self,
        monitor,
        swarm,
        local,
        breaker_threshold: int = 3,
        breaker_cooldown: float = 30.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.monitor = monitor
        self.swarm = swarm
        self.local = local
        self.breaker_threshold = max(1, int(breaker_threshold))
        self.breaker_cooldown = float(breaker_cooldown)
        self._clock = clock
        self._failures = 0
        self._open_until = 0.0
        self._last_error: str | None = None
        self._last_mode: str | None = None

    # ------------------------------------------------------------------ breaker
    def breaker_open(self) -> bool:
        return self._clock() < self._open_until

    def _record_success(self) -> None:
        if self._failures or self._open_until:
            logger.info("swarm recovered, circuit closed")
        self._failures = 0
        self._open_until = 0.0
        self._last_error = None

    def _record_failure(self, error: str) -> None:
        self._failures += 1
        self._last_error = error
        if self._failures >= self.breaker_threshold and not self.breaker_open():
            self._open_until = self._clock() + self.breaker_cooldown
            logger.warning(
                "swarm circuit opened for %.0fs after %d failures: %s",
                self.breaker_cooldown,
                self._failures,
                error,
            )

    # ------------------------------------------------------------------ routing
    def _local_sync_state(self) -> str:
        return SYNC_PENDING if getattr(self.swarm, "can_sync", False) else SYNC_NOT_NEEDED

    async def process(
        self, kind: str, payload: dict, prefer_local: bool = False
    ) -> RouteOutcome:
        not_online = not self.monitor.is_online()

        if prefer_local or not_online:
            note = "requested local" if prefer_local else "device offline"
            result = await self.local.run(kind, payload)
            self._last_mode = MODE_LOCAL
            return RouteOutcome(result, MODE_LOCAL, self._local_sync_state(), note)

        if self.breaker_open():
            result = await self.local.run(kind, payload)
            self._last_mode = MODE_LOCAL
            return RouteOutcome(
                result,
                MODE_LOCAL,
                self._local_sync_state(),
                f"swarm circuit open after failures: {self._last_error}",
            )

        if not getattr(self.swarm, "configured", False):
            result = await self.local.run(kind, payload)
            self._last_mode = MODE_LOCAL
            return RouteOutcome(result, MODE_LOCAL, SYNC_NOT_NEEDED, "swarm not configured")

        try:
            result = await self.swarm.run(kind, payload)
        except ProcessorUnavailable as exc:
            self._record_failure(str(exc))
            fallback = await self.local.run(kind, payload)
            self._last_mode = MODE_LOCAL
            return RouteOutcome(
                result=fallback,
                mode=MODE_LOCAL,
                sync_state=self._local_sync_state(),
                note=f"swarm unavailable: {exc}",
            )
        except Exception as exc:  # unexpected: never lose the user's job
            self._record_failure(f"{type(exc).__name__}: {exc}")
            logger.exception("unexpected swarm failure, falling back to local")
            result_local = await self.local.run(kind, payload)
            self._last_mode = MODE_LOCAL
            return RouteOutcome(
                result_local,
                MODE_LOCAL,
                self._local_sync_state(),
                f"swarm error: {type(exc).__name__}",
            )

        self._record_success()
        self._last_mode = MODE_SWARM
        return RouteOutcome(result, MODE_SWARM, SYNC_NOT_NEEDED, None)

    def snapshot(self) -> dict:
        return {
            "last_mode": self._last_mode,
            "breaker": "open" if self.breaker_open() else "closed",
            "consecutive_failures": self._failures,
            "last_error": self._last_error,
            "reopens_in_seconds": round(max(0.0, self._open_until - self._clock()), 1)
            if self.breaker_open()
            else 0.0,
        }
