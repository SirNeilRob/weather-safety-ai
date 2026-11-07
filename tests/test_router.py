from __future__ import annotations

import pytest

from app.core.db import SYNC_NOT_NEEDED, SYNC_PENDING
from app.core.processors.base import ProcessorUnavailable
from app.core.processors.local import LocalProcessor
from app.core.router import MODE_LOCAL, MODE_SWARM, SmartRouter


class FakeMonitor:
    def __init__(self, online: bool = True) -> None:
        self.online = online

    def is_online(self) -> bool:
        return self.online


class FakeSwarm:
    def __init__(self, *, fail: bool = False, can_sync: bool = True) -> None:
        self.name = "swarm"
        self.configured = True
        self.can_sync = can_sync
        self.fail = fail
        self.calls = 0

    async def run(self, kind: str, payload: dict) -> dict:
        self.calls += 1
        if self.fail:
            raise ProcessorUnavailable("HTTP 503: upstream unavailable")
        return {"text": "remote answer", "engine": "swarm", "offline": False}


def build(online: bool = True, swarm: FakeSwarm | None = None, clock=None):
    swarm = swarm or FakeSwarm()
    router = SmartRouter(
        monitor=FakeMonitor(online),
        swarm=swarm,
        local=LocalProcessor(),
        breaker_threshold=2,
        breaker_cooldown=30.0,
        clock=clock or (lambda: 0.0),
    )
    return router, swarm


@pytest.mark.anyio
async def test_offline_routes_to_local_and_queues_for_sync() -> None:
    router, swarm = build(online=False)
    outcome = await router.process("analyze", {"text": "The pump failed at noon."})

    assert outcome.mode == MODE_LOCAL
    assert outcome.sync_state == SYNC_PENDING
    assert outcome.note == "device offline"
    assert swarm.calls == 0
    assert outcome.result["engine"] == "local:builtin"


@pytest.mark.anyio
async def test_online_routes_to_swarm() -> None:
    router, swarm = build(online=True)
    outcome = await router.process("analyze", {"text": "hello"})

    assert outcome.mode == MODE_SWARM
    assert outcome.sync_state == SYNC_NOT_NEEDED
    assert swarm.calls == 1
    assert outcome.result["text"] == "remote answer"


@pytest.mark.anyio
async def test_prefer_local_skips_swarm_while_online() -> None:
    router, swarm = build(online=True)
    outcome = await router.process("analyze", {"text": "hello"}, prefer_local=True)

    assert outcome.mode == MODE_LOCAL
    assert outcome.note == "requested local"
    assert swarm.calls == 0


@pytest.mark.anyio
async def test_sync_not_queued_when_no_sync_endpoint_exists() -> None:
    router, _ = build(online=False, swarm=FakeSwarm(can_sync=False))
    outcome = await router.process("analyze", {"text": "hello"})
    assert outcome.sync_state == SYNC_NOT_NEEDED


@pytest.mark.anyio
async def test_swarm_failure_falls_back_and_keeps_the_job() -> None:
    router, swarm = build(online=True, swarm=FakeSwarm(fail=True))
    outcome = await router.process(
        "summarize", {"text": "The generator failed. Power was restored after two hours."}
    )

    assert swarm.calls == 1
    assert outcome.mode == MODE_LOCAL
    assert outcome.sync_state == SYNC_PENDING
    assert outcome.result.get("summary")
    assert "swarm unavailable" in (outcome.note or "")
    assert "503" in (outcome.note or "")


@pytest.mark.anyio
async def test_circuit_breaker_stops_hammering_a_broken_swarm() -> None:
    now = [0.0]
    router, swarm = build(online=True, swarm=FakeSwarm(fail=True), clock=lambda: now[0])

    first = await router.process("analyze", {"text": "one"})
    second = await router.process("analyze", {"text": "two"})
    assert swarm.calls == 2
    assert first.mode == second.mode == MODE_LOCAL

    # breaker is now open: third call must not touch Swarm at all
    third = await router.process("analyze", {"text": "three"})
    assert swarm.calls == 2
    assert third.mode == MODE_LOCAL
    assert "circuit open" in (third.note or "")
    assert router.snapshot()["breaker"] == "open"

    # after the cooldown the router tries Swarm again
    now[0] = 31.0
    swarm.fail = False
    fourth = await router.process("analyze", {"text": "four"})
    assert swarm.calls == 3
    assert fourth.mode == MODE_SWARM
    assert router.snapshot()["breaker"] == "closed"
