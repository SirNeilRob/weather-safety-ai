"""Connectivity detection.

Two rules make this usable in the real world:

1. **Never probe on the request path.** A job submission must not wait on a
   link check. Probes run in one background task and callers read the last
   known state.
2. **Hysteresis.** One dropped packet is not "offline". The state only flips
   after N consecutive failures (or successes), which stops the app flapping
   between remote and local processing on a bad hotspot.

The probe deliberately treats any HTTP status below 500 as "link is up". A 401
from the Swarm API still proves the internet works; it is the processor's job
to complain about credentials, not the link monitor's job to go offline.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Awaitable, Callable

import httpx

logger = logging.getLogger(__name__)


class ConnectivityState:
    ONLINE = "online"
    OFFLINE = "offline"
    UNKNOWN = "unknown"


async def http_probe(url: str, timeout: float = 3.0) -> bool:
    """True if the URL answers with anything that is not a server error."""
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(timeout), follow_redirects=True
        ) as client:
            response = await client.get(url)
        return response.status_code < 500
    except (httpx.HTTPError, OSError, ValueError):
        return False


@dataclass(slots=True)
class ProbeResult:
    url: str
    ok: bool
    elapsed_ms: float


class ConnectivityMonitor:
    def __init__(
        self,
        probes: tuple[str, ...] | list[str] = (),
        interval: float = 5.0,
        timeout: float = 3.0,
        offline_after: int = 2,
        online_after: int = 1,
        forced_offline: bool = False,
        probe_fn: Callable[[str, float], Awaitable[bool]] = http_probe,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.probes = tuple(probes)
        self.interval = max(0.2, float(interval))
        self.timeout = float(timeout)
        self.offline_after = max(1, int(offline_after))
        self.online_after = max(1, int(online_after))
        self._forced_offline = bool(forced_offline)
        self._probe_fn = probe_fn
        self._clock = clock

        self._state = ConnectivityState.UNKNOWN
        self._ok_streak = 0
        self._fail_streak = 0
        self._last_probe_at: float | None = None
        self._last_success_at: float | None = None
        self._last_error: str | None = None
        self._results: list[ProbeResult] = []
        self._transitions = 0

        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self._online_event = asyncio.Event()
        self._changed = asyncio.Event()

    # ------------------------------------------------------------------ state
    @property
    def state(self) -> str:
        """Effective state: what the router should believe."""
        if self._forced_offline:
            return ConnectivityState.OFFLINE
        if self._state == ConnectivityState.UNKNOWN:
            return ConnectivityState.OFFLINE
        return self._state

    @property
    def raw_state(self) -> str:
        return self._state

    @property
    def forced_offline(self) -> bool:
        return self._forced_offline

    def is_online(self) -> bool:
        return self.state == ConnectivityState.ONLINE

    @property
    def online_event(self) -> asyncio.Event:
        return self._online_event

    @property
    def transitions(self) -> int:
        return self._transitions

    # ------------------------------------------------------------------ probing
    async def probe_once(self) -> bool:
        self._last_probe_at = self._clock()
        self._results = []
        if not self.probes:
            self._last_error = "no probe urls configured"
            self._apply(False)
            return False

        any_ok = False
        for url in self.probes:
            started = self._clock()
            try:
                ok = bool(await self._probe_fn(url, self.timeout))
            except Exception as exc:  # a probe must never kill the loop
                ok = False
                self._last_error = f"{url}: {exc}"
            elapsed = (self._clock() - started) * 1000
            self._results.append(ProbeResult(url=url, ok=ok, elapsed_ms=round(elapsed, 1)))
            any_ok = any_ok or ok
            if any_ok:
                break  # first success is enough

        if any_ok:
            self._last_success_at = self._clock()
            self._last_error = None
        elif self._last_error is None:
            self._last_error = "all probes failed"
        self._apply(any_ok)
        return any_ok

    def _apply(self, ok: bool) -> None:
        previous = self.state
        if ok:
            self._ok_streak += 1
            self._fail_streak = 0
            if self._ok_streak >= self.online_after:
                self._state = ConnectivityState.ONLINE
        else:
            self._fail_streak += 1
            self._ok_streak = 0
            if self._fail_streak >= self.offline_after:
                self._state = ConnectivityState.OFFLINE

        current = self.state
        if current == ConnectivityState.ONLINE:
            self._online_event.set()
        else:
            self._online_event.clear()

        if current != previous:
            self._transitions += 1
            self._changed.set()
            logger.info(
                "connectivity %s -> %s (ok_streak=%d fail_streak=%d)",
                previous,
                current,
                self._ok_streak,
                self._fail_streak,
            )

    async def wait_online(self, timeout: float | None = None) -> bool:
        try:
            await asyncio.wait_for(self._online_event.wait(), timeout=timeout)
            return True
        except (asyncio.TimeoutError, TimeoutError):
            return False

    def set_forced_offline(self, enabled: bool) -> None:
        """Demo switch: behave exactly like an offline device."""
        previous = self.state
        self._forced_offline = bool(enabled)
        current = self.state
        if current == ConnectivityState.ONLINE:
            self._online_event.set()
        else:
            self._online_event.clear()
        if current != previous:
            self._transitions += 1
            logger.info("connectivity forced %s -> %s", previous, current)

    # ------------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._run(), name="connectivity-monitor")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                await self.probe_once()
            except Exception:  # pragma: no cover - defensive
                logger.exception("connectivity probe crashed")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.interval)
            except (asyncio.TimeoutError, TimeoutError):
                continue

    # ------------------------------------------------------------------ reporting
    def snapshot(self) -> dict:
        return {
            "state": self.state,
            "raw_state": self._state,
            "forced": self._forced_offline,
            "probes": [
                {"url": r.url, "ok": r.ok, "elapsed_ms": r.elapsed_ms}
                for r in self._results
            ],
            "last_error": self._last_error,
            "transitions": self._transitions,
        }
