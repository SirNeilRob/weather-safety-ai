from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app

SAMPLE = (
    "The generator at the north site failed at 03:12. "
    "Field crew restored power after replacing the fuel pump. "
    "The outage affected 412 homes for 96 minutes. "
    "Contact dispatch@example.com for details."
)


@pytest.fixture
def client_factory(tmp_path):
    clients: list[TestClient] = []

    def _make(**overrides) -> TestClient:
        settings = Settings(
            db_path=tmp_path / f"api{len(clients)}.db",
            probe_urls=(),
            probe_on_startup=False,
            probe_interval=3600.0,
            forced_offline=overrides.pop("forced_offline", True),
            worker_count=2,
            worker_idle_poll=0.05,
            job_timeout=10.0,
            sync_interval=3600.0,
            **overrides,
        )
        test_client = TestClient(create_app(settings))
        test_client.__enter__()
        clients.append(test_client)
        return test_client

    yield _make

    for test_client in clients:
        test_client.__exit__(None, None, None)


def wait_for_job(client: TestClient, job_id: str, timeout: float = 5.0) -> dict:
    deadline = time.monotonic() + timeout
    payload: dict = {}
    while time.monotonic() < deadline:
        response = client.get(f"/v1/jobs/{job_id}")
        assert response.status_code == 200
        payload = response.json()
        if payload["status"] in ("done", "failed"):
            return payload
        time.sleep(0.05)
    return payload


def test_health_reports_offline_without_erroring(client_factory) -> None:
    client = client_factory()
    response = client.get("/v1/health")

    assert response.status_code == 200
    body = response.json()
    assert body["ok"] is True
    assert body["mode"] == "offline"
    assert body["connectivity"]["state"] == "offline"
    assert body["queue"]["total"] == 0
    assert body["local"]["engine"] == "local:builtin"


def test_job_is_processed_while_offline(client_factory) -> None:
    client = client_factory()
    response = client.post(
        "/v1/jobs",
        json={"kind": "analyze", "payload": {"text": SAMPLE}, "wait_seconds": 5},
    )

    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "done"
    assert body["engine"] == "local"
    assert "summary" in body["result"]
    assert body["result"]["offline"] is True
    assert "dispatch@example.com" in body["result"]["entities"]["emails"]


def test_submit_returns_immediately_and_can_be_polled(client_factory) -> None:
    client = client_factory()
    started = time.monotonic()
    response = client.post("/v1/jobs", json={"kind": "summarize", "payload": {"text": SAMPLE}})
    elapsed = time.monotonic() - started

    assert response.status_code == 201
    assert elapsed < 1.0  # the HTTP call never waits for the processor
    job_id = response.json()["id"]

    final = wait_for_job(client, job_id)
    assert final["status"] == "done"
    assert final["engine"] == "local"
    assert final["result"]["summary"]


def test_job_list_and_capabilities(client_factory) -> None:
    client = client_factory()
    for _ in range(3):
        client.post("/v1/jobs", json={"kind": "extract", "payload": {"text": SAMPLE}, "wait_seconds": 5})

    listing = client.get("/v1/jobs").json()
    assert len(listing) == 3
    assert all(job["status"] == "done" for job in listing)

    capabilities = client.get("/v1/capabilities").json()
    assert capabilities["offline_capable"] is True
    assert capabilities["engines"]["local"] is True
    assert "summarize" in capabilities["kinds"]


def test_unknown_job_returns_404(client_factory) -> None:
    client = client_factory()
    assert client.get("/v1/jobs/nope").status_code == 404


def test_invalid_inputs_are_rejected(client_factory) -> None:
    client = client_factory()

    assert client.get("/v1/jobs?status=banana").status_code == 422
    assert client.post("/v1/jobs", json={"kind": "x" * 80}).status_code == 422
    assert (
        client.post("/v1/jobs", json={"payload": {"text": "a" * 1_000_001}}).status_code
        == 413
    )


def test_demo_switch_toggles_offline_mode(client_factory) -> None:
    client = client_factory(forced_offline=False)

    enabled = client.post("/v1/demo/offline", json={"enabled": True}).json()
    assert enabled["forced_offline"] is True
    assert enabled["connectivity"]["state"] == "offline"

    disabled = client.post("/v1/demo/offline", json={"enabled": False}).json()
    assert disabled["forced_offline"] is False


def test_sync_endpoint_reports_disabled_without_configuration(client_factory) -> None:
    client = client_factory()
    body = client.post("/v1/sync/run").json()
    assert body["status"] == "disabled"
