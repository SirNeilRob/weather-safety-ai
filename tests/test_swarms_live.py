"""Live integration tests against the real Swarms API (no stand-ins).

They read SWARMS_API_KEY from the environment or the repo's .env and are
skipped when it is missing. Each run costs well under one US cent.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import Settings, load_env_file
from app.core.processors.swarm import SwarmProcessor
from app.main import create_app

ROOT = Path(__file__).resolve().parent.parent
load_env_file(ROOT / ".env")
KEY = os.getenv("SWARMS_API_KEY") or os.getenv("SWARM_API_KEY")
BASE = "https://api.swarms.world"

pytestmark = pytest.mark.skipif(not KEY, reason="SWARMS_API_KEY not set")

TORNADO = {
    "event": "Tornado Warning",
    "severity": "Extreme",
    "urgency": "Immediate",
    "certainty": "Observed",
    "areaDesc": "Cedar Rapids, Linn County, Iowa",
    "headline": "Tornado Warning for Linn County",
    "description": "A confirmed tornado is moving toward Cedar Rapids.",
    "instruction": "Take shelter now in a basement or interior room.",
}


@pytest.fixture
def client(tmp_path):
    settings = Settings(
        db_path=tmp_path / "live.db",
        swarm_base_url=BASE,
        swarm_api_key=KEY,
        probe_urls=(f"{BASE}/health",),
        alert_cloud_stages=("communication",),
        alert_cloud_timeout=45.0,
        probe_interval=30.0,
    )
    with TestClient(create_app(settings)) as test_client:
        yield test_client


@pytest.mark.anyio
async def test_swarms_agent_endpoint_answers() -> None:
    swarm = SwarmProcessor(base_url=BASE, api_key=KEY, timeout=60)
    result = await swarm.run("analyze", {"prompt": "Reply with the single word: ready"})
    assert result["engine"] == "swarm"
    assert "ready" in result["text"].lower()


def test_backend_reports_online_against_real_swarms(client) -> None:
    health = client.get("/v1/health").json()
    assert health["mode"] == "online"
    assert health["swarm"]["configured"] is True
    assert health["alert_pipeline"]["cloud_ready"] is True


def test_online_alert_gets_swarm_copy_with_local_safety(client) -> None:
    job = client.post("/v1/alerts", json={"alert": TORNADO, "wait_seconds": 60}).json()
    assert job["status"] == "done", job
    result = job["result"]

    # Deterministic stages are untouched by the model.
    assert result["classification"]["hazard"] == "tornado"
    assert result["classification"]["severity"] == "extreme"
    assert result["safety"]["first_action"]["es"]
    assert [s["engine"] for s in result["trace"][:2]] == ["local", "local"]

    communication = result["communication"]
    # Official titles are always the curated ones.
    assert communication["title"]["es"].startswith("Aviso de tornado")
    for language in ("en", "es"):
        assert communication["sms"][language]["chars"] <= 160
    assert "advertencia" not in communication["sms"]["es"]["text"].lower()

    # Swarm either wrote the copy (mixed) or was rejected and the local
    # composer answered; both are valid, and the job says which.
    assert result["pipeline_engine"] in ("mixed", "local")
    assert job["engine"] == ("swarm" if result["pipeline_engine"] == "mixed" else "local")


def test_forced_offline_gives_identical_classification(client) -> None:
    online = client.post("/v1/alerts", json={"alert": TORNADO, "wait_seconds": 60}).json()
    client.post("/v1/demo/offline", json={"enabled": True})
    offline = client.post("/v1/alerts", json={"alert": TORNADO, "wait_seconds": 10}).json()

    assert offline["result"]["pipeline_engine"] == "local"
    assert offline["result"]["communication"]["title"] == online["result"]["communication"]["title"]
    for key in ("hazard", "severity", "product"):
        assert offline["result"]["classification"][key] == online["result"]["classification"][key]
    assert offline["result"]["safety"]["immediate"] == online["result"]["safety"]["immediate"]
