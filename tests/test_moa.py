"""Mixture-of-Agents communication stage: routing, fallbacks and wire format."""

from __future__ import annotations

import json

import httpx
import pytest

from app.agents.cloud import CloudDrafter
from app.agents.pipeline import AlertPipeline
from app.core.processors.base import ProcessorUnavailable
from app.core.processors.swarm import SwarmProcessor
from tests.fixtures.samples import SAMPLES
from tests.test_alert_pipeline import cloud_payload


def sample(name: str) -> dict:
    return next(entry for entry in SAMPLES if entry["name"] == name)


class _Monitor:
    def is_online(self) -> bool:
        return True


class _StubSwarm:
    configured = True

    def __init__(self, moa=None, single=None, moa_error: Exception | None = None) -> None:
        self.moa, self.single, self.moa_error = moa, single, moa_error
        self.calls: list[str] = []

    async def draft_communication_moa(self, timeout=None, **kwargs):
        self.calls.append("moa")
        if self.moa_error:
            raise self.moa_error
        return self.moa, {
            "strategy": "moa",
            "experts": ["EN-Writer", "ES-Writer", "Fact-Checker"],
            "aggregator": "Editor",
        }

    async def draft_communication(self, **kwargs):
        self.calls.append("single")
        if self.single is None:
            raise ProcessorUnavailable("down")
        return self.single


def _drafter(swarm, **kwargs) -> CloudDrafter:
    return CloudDrafter(swarm, _Monitor(), strategy="moa", timeout=25.0, **kwargs)


@pytest.mark.anyio
async def test_moa_writes_the_copy_and_the_trace_names_the_agents() -> None:
    swarm = _StubSwarm(moa=cloud_payload())
    result = await AlertPipeline(cloud=_drafter(swarm)).run(sample("tornado_emergency")["alert"])

    assert swarm.calls == ["moa"]
    step = result["trace"][-1]
    assert step["engine"] == "swarm"
    assert step["detail"]["cloud"]["strategy"] == "moa"
    assert step["detail"]["cloud"]["aggregator"] == "Editor"
    assert any("Mixture of Agents" in w for w in result["warnings"])
    # The deterministic envelope still wins over the model.
    assert result["communication"]["severity"]["level"] == "extreme"


@pytest.mark.anyio
async def test_moa_failure_falls_back_to_a_single_agent() -> None:
    swarm = _StubSwarm(single=cloud_payload(), moa_error=ProcessorUnavailable("editor broke"))
    result = await AlertPipeline(cloud=_drafter(swarm)).run(sample("tornado_emergency")["alert"])

    assert swarm.calls == ["moa", "single"]
    cloud = result["trace"][-1]["detail"]["cloud"]
    assert cloud["strategy"] == "single"
    assert "editor broke" in cloud["moa_fallback"]


@pytest.mark.anyio
async def test_moa_copy_with_wrong_spanish_product_word_is_rejected() -> None:
    bad = cloud_payload()
    bad["sms"]["es"]["text"] = "Advertencia de tornado. Refúgiese."
    swarm = _StubSwarm(moa=bad, single=cloud_payload())
    result = await AlertPipeline(cloud=_drafter(swarm)).run(sample("tornado_emergency")["alert"])

    # Same gate as the single-agent path: MoA output is not trusted more.
    assert swarm.calls == ["moa", "single"]
    assert result["trace"][-1]["detail"]["cloud"]["strategy"] == "single"


@pytest.mark.anyio
async def test_everything_cloud_failing_still_delivers_local_copy() -> None:
    swarm = _StubSwarm(moa_error=ProcessorUnavailable("down"), single=None)
    result = await AlertPipeline(cloud=_drafter(swarm)).run(sample("tornado_emergency")["alert"])

    assert result["engine"] == "local"
    assert result["communication"]["sms"]["en"]["chars"] <= 160


@pytest.mark.anyio
async def test_low_severity_alerts_skip_moa_to_save_cost() -> None:
    low = {"identifier": "LOW-1", "event": "Dense Fog Advisory", "severity": "Minor",
           "urgency": "Expected", "certainty": "Likely", "areaDesc": "Miami-Dade County",
           "headline": "Dense Fog Advisory", "description": "Patchy fog this morning."}
    swarm = _StubSwarm(moa=cloud_payload(), single=cloud_payload())
    drafter = _drafter(swarm, moa_min_severity="severe")
    result = await AlertPipeline(cloud=drafter).run(low)

    assert swarm.calls == ["single"]
    assert "moa_skipped" in result["trace"][-1]["detail"]["cloud"]


def test_single_strategy_never_uses_moa() -> None:
    drafter = CloudDrafter(_StubSwarm(), _Monitor(), strategy="single")
    assert drafter.use_moa({"severity": "extreme"}) is False


# --------------------------------------------------------------------------- wire format
def _swarm_reply(editor_content: str) -> dict:
    """Shape captured from the live API (2026-11-07)."""
    return {
        "job_id": "job-1",
        "status": "success",
        "swarm_type": "MixtureOfAgents",
        "output": [
            {"role": "System", "content": "Team Name: ResQ ..."},
            {"role": "EN-Writer", "content": "Seek shelter now."},
            {"role": "ES-Writer", "content": "Busque refugio ahora."},
            {"role": "Fact-Checker", "content": "- MUST KEEP: area"},
            {"role": "Editor", "content": editor_content},
        ],
        "usage": {"total_tokens": 900, "total_cost": 0.04},
    }


@pytest.mark.anyio
async def test_moa_request_and_response_round_trip() -> None:
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json=_swarm_reply(json.dumps(cloud_payload())))

    swarm = SwarmProcessor("https://api.swarms.world", api_key="k",
                           transport=httpx.MockTransport(handler))
    payload, meta = await swarm.draft_communication_moa(
        alert={"event": "Tornado Warning"},
        classification={"hazard": "tornado", "severity": "extreme", "product": "warning"},
        safety={"immediate": [{"en": "Go to a basement."}], "avoid": []},
        official_title={"en": "Tornado Warning", "es": "Aviso de tornado"},
    )

    assert seen["path"] == "/v1/swarm/completions"
    assert seen["body"]["swarm_type"] == "MixtureOfAgents"
    names = [a["agent_name"] for a in seen["body"]["agents"]]
    assert names == ["EN-Writer", "ES-Writer", "Fact-Checker", "Editor"]  # editor last = aggregator
    assert payload["title"]["es"] == "Aviso de tornado"
    assert meta["aggregator"] == "Editor"
    assert meta["usage"]["cost_usd"] == 0.04
    assert swarm.last_call["ok"] is True


@pytest.mark.anyio
async def test_moa_editor_prose_instead_of_json_is_an_error() -> None:
    transport = httpx.MockTransport(lambda r: httpx.Response(200, json=_swarm_reply("Sorry, I can't.")))
    swarm = SwarmProcessor("https://api.swarms.world", api_key="k", transport=transport)
    with pytest.raises(ProcessorUnavailable):
        await swarm.draft_communication_moa(
            alert={}, classification={}, safety={}, official_title={}
        )
