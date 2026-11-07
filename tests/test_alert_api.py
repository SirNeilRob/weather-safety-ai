from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from tests.fixtures.samples import SAMPLES
from app.main import create_app


@pytest.fixture(scope="module")
def client():
    """Offline app instance: the offline path is the one that must always work."""
    settings = Settings(
        probe_urls=(),
        probe_on_startup=False,
        probe_interval=3600.0,
        forced_offline=True,
        worker_count=2,
        worker_idle_poll=0.05,
        job_timeout=10.0,
        sync_interval=3600.0,
        db_path="/tmp/sharkbyte_alert_api.db",
    )
    with TestClient(create_app(settings)) as test_client:
        yield test_client


def submit(client: TestClient, alert: dict) -> dict:
    response = client.post("/v1/alerts", json={"alert": alert, "wait_seconds": 20})
    assert response.status_code == 201, response.text
    return response.json()


def test_alert_endpoint_returns_the_finished_pipeline_result(client) -> None:
    body = submit(client, SAMPLES[0]["alert"])
    assert body["status"] == "done"
    assert body["engine"] == "local"
    result = body["result"]
    assert result["classification"]["hazard"] == "tornado"
    assert result["classification"]["severity"] == "extreme"
    assert result["safety"]["first_action"]["es"]
    assert result["communication"]["sms"]["en"]["segments"] >= 1
    assert [step["agent"] for step in result["trace"]] == ["weather", "safety", "communication"]


def test_alert_is_persisted_and_readable_by_id(client) -> None:
    created = submit(client, SAMPLES[1]["alert"])
    fetched = client.get(f"/v1/jobs/{created['id']}").json()
    assert fetched["id"] == created["id"]
    assert fetched["result"]["classification"]["hazard"] == "flood"


def test_empty_alert_is_rejected(client) -> None:
    response = client.post("/v1/alerts", json={"alert": {}, "wait_seconds": 1})
    assert response.status_code == 422


def test_canned_samples_are_not_served(client) -> None:
    assert client.get("/v1/alerts/samples").status_code == 404


def test_hazard_legend_covers_both_languages_and_severity_levels(client) -> None:
    body = client.get("/v1/hazards").json()
    assert [level["key"] for level in body["severity_levels"]] == [
        "unknown",
        "minor",
        "moderate",
        "severe",
        "extreme",
    ]
    assert all(level["es"] and level["color"].startswith("#") for level in body["severity_levels"])
    assert {hazard["key"] for hazard in body["hazards"]} >= {"tornado", "flood", "heat"}
    assert {language["code"] for language in body["languages"]} == {"en", "es"}


def test_queue_summary_reports_durable_state(client) -> None:
    before = client.get("/v1/queue/summary").json()
    submit(client, SAMPLES[0]["alert"])
    after = client.get("/v1/queue/summary").json()
    assert after["queue"]["total"] >= before["queue"]["total"] + 1
    assert set(after["queue"]) >= {"total", "pending", "running", "done", "failed", "pending_sync"}


def test_spanish_only_request_still_returns_english_for_the_operator(client) -> None:
    response = client.post(
        "/v1/alerts",
        json={"alert": SAMPLES[9]["alert"], "languages": ["es"], "wait_seconds": 20},
    )
    assert response.status_code == 201
    result = response.json()["result"]
    assert result["languages"] == ["es"]
    assert result["communication"]["sms"]["es"]["text"]


def test_active_alerts_lists_processed_alerts_and_drops_expired(client) -> None:
    live = submit(client, {"event": "Flash Flood Warning", "areaDesc": "Travis County",
                           "expires": "2099-01-01T00:00:00Z"})
    expired = submit(client, {"event": "Heat Advisory", "areaDesc": "Miami-Dade County",
                              "expires": "2000-01-01T00:00:00Z"})

    body = client.get("/v1/alerts/active").json()
    ids = [alert["id"] for alert in body["alerts"]]
    assert live["id"] in ids
    assert expired["id"] not in ids
    assert body["alerts"][0]["result"]["classification"]["hazard"]


def test_dashboard_is_served_by_the_backend(client) -> None:
    page = client.get("/")
    assert page.status_code == 200
    assert "ResQ" in page.text
    assert client.get("/v1").json()["health"] == "/v1/health"
