"""Watch list: Florida ZIPs, max 5 cities, one card per alert across cities.

Runs offline. NWS is replaced by an httpx.MockTransport inside these tests only;
nothing here is reachable from app/.
"""

from __future__ import annotations

import time

import httpx
import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app

ALERT_ID = "urn:oid:test.shared.alert"


@pytest.fixture
def client(tmp_path):
    settings = Settings(
        probe_urls=(),
        probe_on_startup=False,
        probe_interval=3600.0,
        forced_offline=False,
        worker_count=2,
        worker_idle_poll=0.05,
        job_timeout=10.0,
        sync_interval=3600.0,
        noaa_poll_interval=3600.0,
        db_path=tmp_path / "watch.db",
    )
    with TestClient(create_app(settings)) as test_client:
        yield test_client


def _feed(client):
    return client.app.state.ctx.noaa


def test_empty_watch_list_on_first_load(client) -> None:
    body = client.get("/v1/watchlist").json()
    assert body == {"cities": [], "max_cities": 5}
    assert client.get("/v1/noaa").json()["cities"] == []
    assert client.post("/v1/noaa/poll").json()["status"] == "skipped"


def test_valid_florida_zip_is_added_with_census_coordinates(client) -> None:
    response = client.post("/v1/watchlist", json={"zip": "33130"})
    assert response.status_code == 201, response.text
    city = response.json()["city"]
    assert city["zip"] == "33130" and city["key"] == "33130"
    assert (city["lat"], city["lon"]) == (25.7685, -80.2034)  # Census INTPTLAT/INTPTLONG, rounded
    assert city["name"] is None  # offline here: no NWS name, so none is invented
    assert city["added_at"]
    assert [c["zip"] for c in client.get("/v1/watchlist").json()["cities"]] == ["33130"]


@pytest.mark.parametrize(
    ("zip_code", "status", "detail"),
    [
        ("33999", 404, "ZIP not found"),        # Florida range, no such ZCTA
        ("90210", 422, "Florida ZIPs only for now"),
        ("3313", 422, "ZIP must be 5 digits"),
    ],
)
def test_invalid_zip_is_rejected(client, zip_code, status, detail) -> None:
    response = client.post("/v1/watchlist", json={"zip": zip_code})
    assert response.status_code == status
    assert response.json()["detail"] == detail
    assert client.get("/v1/watchlist").json()["cities"] == []


def test_sixth_city_is_rejected(client) -> None:
    for zip_code in ("33130", "32801", "33602", "32301", "33901"):
        assert client.post("/v1/watchlist", json={"zip": zip_code}).status_code == 201
    sixth = client.post("/v1/watchlist", json={"zip": "32202"})
    assert sixth.status_code == 409
    assert sixth.json()["detail"] == "Max 5 cities"
    point = client.post("/v1/watchlist/point", json={"latitude": 27.0, "longitude": -81.0, "name": "x"})
    assert point.status_code == 409
    assert len(client.get("/v1/watchlist").json()["cities"]) == 5


def test_remove_city(client) -> None:
    client.post("/v1/watchlist", json={"zip": "33130"})
    point = client.post("/v1/watchlist/point",
                        json={"latitude": 27.1, "longitude": -81.2, "name": "Glades County"}).json()["city"]
    assert point["key"] == "fl1" and point["name"] == "Glades County"
    assert client.delete("/v1/watchlist/33130").json()["cities"][0]["key"] == "fl1"
    assert client.delete("/v1/watchlist/fl1").json()["cities"] == []
    assert client.delete("/v1/watchlist/fl1").status_code == 404


def test_watch_list_survives_restart(tmp_path) -> None:
    settings = Settings(probe_urls=(), probe_on_startup=False, sync_interval=3600.0,
                        noaa_poll_interval=3600.0, db_path=tmp_path / "watch.db")
    with TestClient(create_app(settings)) as first:
        first.post("/v1/watchlist", json={"zip": "33130"})
    with TestClient(create_app(settings)) as second:
        assert [c["zip"] for c in second.get("/v1/watchlist").json()["cities"]] == ["33130"]


class _Online:
    def is_online(self) -> bool:
        return True


def _nws_stub(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path == "/alerts/active":
        return httpx.Response(200, json={"features": [{"properties": {
            "id": ALERT_ID, "status": "Actual", "messageType": "Alert",
            "event": "Flood Warning", "severity": "Severe", "urgency": "Immediate",
            "certainty": "Likely", "areaDesc": "Test area", "headline": "Flood Warning",
            "expires": "2099-01-01T00:00:00Z",
        }}]})
    if path.startswith("/points/"):
        return httpx.Response(200, json={"properties": {}})
    return httpx.Response(404, json={})


def test_same_alert_in_two_cities_is_one_card(client) -> None:
    client.post("/v1/watchlist", json={"zip": "33130"})
    client.post("/v1/watchlist", json={"zip": "32801"})
    feed = _feed(client)
    feed.monitor = _Online()
    feed.transport = httpx.MockTransport(_nws_stub)

    poll = client.post("/v1/noaa/poll").json()
    assert poll["status"] == "ok" and poll["new"] == 1 and poll["active"] == 1

    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        alerts = client.get("/v1/alerts/active").json()["alerts"]
        if alerts:
            break
        time.sleep(0.1)
    cards = [a for a in alerts if a["result"]["alert"]["id"] == ALERT_ID]
    assert len(cards) == 1
    assert sorted(c["zip"] for c in cards[0]["cities"]) == ["32801", "33130"]
