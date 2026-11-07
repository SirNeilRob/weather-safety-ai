from __future__ import annotations

import pytest

from app.core.connectivity import ConnectivityMonitor, ConnectivityState


class ScriptedProbe:
    def __init__(self, answers: list[bool | Exception]) -> None:
        self.answers = list(answers)
        self.calls = 0

    async def __call__(self, url: str, timeout: float) -> bool:
        self.calls += 1
        answer = self.answers.pop(0) if self.answers else False
        if isinstance(answer, Exception):
            raise answer
        return answer


@pytest.mark.anyio
async def test_unknown_state_is_treated_as_offline() -> None:
    monitor = ConnectivityMonitor(probes=("http://probe",))
    assert monitor.raw_state == ConnectivityState.UNKNOWN
    assert monitor.is_online() is False


@pytest.mark.anyio
async def test_hysteresis_prevents_flapping() -> None:
    probe = ScriptedProbe([True, False, False, True])
    monitor = ConnectivityMonitor(
        probes=("http://probe",),
        offline_after=2,
        online_after=1,
        probe_fn=probe,
    )

    assert await monitor.probe_once() is True
    assert monitor.is_online() is True

    # one failed probe must not flip the state
    assert await monitor.probe_once() is False
    assert monitor.is_online() is True

    # second consecutive failure flips it
    assert await monitor.probe_once() is False
    assert monitor.state == ConnectivityState.OFFLINE

    # a single success brings it back
    assert await monitor.probe_once() is True
    assert monitor.is_online() is True
    assert monitor.transitions >= 3


@pytest.mark.anyio
async def test_probe_exceptions_are_swallowed() -> None:
    probe = ScriptedProbe([RuntimeError("dns exploded")])
    monitor = ConnectivityMonitor(
        probes=("http://probe",), offline_after=1, probe_fn=probe
    )
    assert await monitor.probe_once() is False
    assert monitor.state == ConnectivityState.OFFLINE
    assert "dns exploded" in (monitor.snapshot()["last_error"] or "")


@pytest.mark.anyio
async def test_forced_offline_overrides_a_healthy_link() -> None:
    probe = ScriptedProbe([True, True])
    monitor = ConnectivityMonitor(probes=("http://probe",), probe_fn=probe)
    await monitor.probe_once()
    assert monitor.is_online() is True

    monitor.set_forced_offline(True)
    assert monitor.is_online() is False
    assert monitor.snapshot()["forced"] is True
    # the raw link state is still reported, which matters for honest UI copy
    assert monitor.raw_state == ConnectivityState.ONLINE

    monitor.set_forced_offline(False)
    assert monitor.is_online() is True


@pytest.mark.anyio
async def test_no_probe_urls_means_offline() -> None:
    monitor = ConnectivityMonitor(probes=())
    assert await monitor.probe_once() is False
    assert monitor.is_online() is False
