"""Live tests against NOAA's National Weather Service API (api.weather.gov).

No key is needed. Skipped when the API cannot be reached.
"""

from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient

from app.agents.pipeline import AlertPipeline
from app.config import Settings
from app.core.noaa import NWS_BASE, to_pipeline_alert
from app.main import create_app

UA = Settings().noaa_user_agent
HEADERS = {"User-Agent": UA, "Accept": "application/geo+json"}
MIAMI = (25.76, -80.19)


def _reachable() -> bool:
    try:
        return httpx.get(f"{NWS_BASE}/alerts/types", headers=HEADERS, timeout=10).status_code == 200
    except httpx.HTTPError:
        return False


pytestmark = pytest.mark.skipif(not _reachable(), reason="api.weather.gov unreachable")


@pytest.fixture
def client(tmp_path):
    settings = Settings(
        db_path=tmp_path / "noaa.db",
        swarm_base_url=None,
        probe_urls=(f"{NWS_BASE}/alerts/types",),
        noaa_latitude=MIAMI[0],
        noaa_longitude=MIAMI[1],
        noaa_poll_interval=3600.0,
        worker_idle_poll=0.05,
    )
    with TestClient(create_app(settings)) as test_client:
        yield test_client


def test_real_nws_alerts_run_through_the_pipeline() -> None:
    """Every currently active US alert type we can see must classify cleanly."""
    data = httpx.get(
        f"{NWS_BASE}/alerts/active?status=actual&message_type=alert",
        headers=HEADERS,
        timeout=30,
    ).json()
    features = data.get("features") or []
    if not features:
        pytest.skip("no active NWS alerts nationwide right now")

    import asyncio

    pipeline = AlertPipeline()
    seen_events = set()
    for feature in features:
        props = feature["properties"]
        if props["event"] in seen_events:
            continue
        seen_events.add(props["event"])
        result = asyncio.run(pipeline.run(to_pipeline_alert(props)))
        assert result["alert"]["id"] == props["id"]
        assert result["classification"]["severity"] in (
            "unknown", "minor", "moderate", "severe", "extreme"
        )
        assert result["safety"]["first_action"]["en"]
        assert result["safety"]["first_action"]["es"]
        assert result["communication"]["sms"]["es"]["text"]
        if len(seen_events) >= 15:
            break


def test_backend_polls_noaa_for_the_location(client) -> None:
    poll = client.post("/v1/noaa/poll").json()
    assert poll["status"] == "ok", poll

    status = client.get("/v1/noaa").json()
    assert status["point"] == {"latitude": MIAMI[0], "longitude": MIAMI[1]}
    assert status["last_error"] is None
    observation = status["observation"]
    assert observation and observation["station"]["id"]
    assert observation["observed_at"]

    # Every alert NWS lists for the point reaches the dashboard feed, processed.
    if poll["active"]:
        import time

        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            active = client.get("/v1/alerts/active").json()["alerts"]
            if len(active) >= poll["active"]:
                break
            time.sleep(0.2)
        assert len(active) == poll["active"]
        assert all(job["result"]["classification"]["hazard"] for job in active)

    # A second poll does not queue the same alerts again.
    again = client.post("/v1/noaa/poll").json()
    assert again["new"] == 0


def test_moving_the_location_replaces_the_noaa_picture(client) -> None:
    client.post("/v1/noaa/poll")
    moved = client.post(
        "/v1/noaa/location", json={"latitude": 41.9779, "longitude": -91.6656, "label": "Cedar Rapids"}
    ).json()
    assert moved["moved"] is True
    assert moved["observation"] is None
    assert moved["polled"] is False
