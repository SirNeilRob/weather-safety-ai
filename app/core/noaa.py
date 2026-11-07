"""NOAA / National Weather Service feed.

Pulls two things for one location from ``api.weather.gov`` (free, no key, but
NWS requires an identifying User-Agent):

* **Active alerts** (``/alerts/active?point=lat,lon``). Each new alert is queued
  through the normal Weather -> Safety -> Communication pipeline exactly once.
* **Current conditions** from the nearest observation station
  (``/points`` -> ``observationStations`` -> ``observations/latest``).

NWS replaces alerts by issuing new messages (updates, cancellations) and drops
them from the active list when they end, so the active list itself is treated
as the truth: the ids from the last successful poll decide which NOAA alerts
the dashboard shows. That set and the last observation are saved next to the
database, so after a reboot with no internet the board still shows the last
known official picture.
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger(__name__)

NWS_BASE = "https://api.weather.gov"
SOURCE = "noaa"


def _round(value: float) -> float:
    # NWS rejects points with more than 4 decimal places.
    return round(float(value), 4)


def _c_to_f(value: Any) -> float | None:
    return round(value * 9 / 5 + 32, 1) if isinstance(value, (int, float)) else None


def _kmh_to_mph(value: Any) -> float | None:
    return round(value * 0.621371, 1) if isinstance(value, (int, float)) else None


def _value(block: Any) -> Any:
    return block.get("value") if isinstance(block, dict) else None


def to_pipeline_alert(props: dict) -> dict:
    """Map NWS alert properties onto the backend's alert input.

    ``ends`` (when the hazard is over) is preferred to ``expires`` (when this
    message is due to be refreshed): if the board goes offline, an alert must
    not vanish just because NWS could not be asked for its next update.
    """
    return {
        "identifier": props.get("id"),
        "event": props.get("event"),
        "severity": props.get("severity"),
        "urgency": props.get("urgency"),
        "certainty": props.get("certainty"),
        "areaDesc": props.get("areaDesc"),
        "headline": props.get("headline"),
        "description": props.get("description"),
        "instruction": props.get("instruction"),
        "sent": props.get("sent"),
        "onset": props.get("onset") or props.get("effective"),
        "expires": props.get("ends") or props.get("expires"),
        "senderName": props.get("senderName"),
        "category": props.get("category"),
        "language": "en",
    }


class NoaaFeed:
    def __init__(
        self,
        store,
        monitor,
        wake,
        state_path: str | Path,
        user_agent: str,
        interval: float = 60.0,
        default_point: tuple[float, float] | None = None,
        timeout: float = 15.0,
        base_url: str = NWS_BASE,
    ) -> None:
        self.store = store
        self.monitor = monitor
        self.wake = wake
        self.state_path = Path(state_path)
        self.user_agent = user_agent
        self.interval = max(15.0, float(interval))
        self.timeout = float(timeout)
        self.base_url = base_url.rstrip("/")

        self.point: tuple[float, float] | None = None
        self.label: str | None = None
        self.current_ids: set[str] = set()
        self.polled: bool = False
        self.observation: dict | None = None
        self.station: dict | None = None
        self.last_poll_at: str | None = None
        self.last_error: str | None = None
        self.ingested = 0

        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._kick = asyncio.Event()
        self._lock = asyncio.Lock()

        self._load()
        if self.point is None and default_point is not None:
            self.point = (_round(default_point[0]), _round(default_point[1]))

    # ------------------------------------------------------------------ state file
    def _load(self) -> None:
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        point = data.get("point")
        if isinstance(point, list) and len(point) == 2:
            self.point = (float(point[0]), float(point[1]))
        self.label = data.get("label")
        self.current_ids = set(data.get("current_ids") or [])
        self.polled = bool(data.get("polled"))
        self.observation = data.get("observation")
        self.station = data.get("station")
        self.last_poll_at = data.get("last_poll_at")

    def _save(self) -> None:
        data = {
            "point": list(self.point) if self.point else None,
            "label": self.label,
            "current_ids": sorted(self.current_ids),
            "polled": self.polled,
            "observation": self.observation,
            "station": self.station,
            "last_poll_at": self.last_poll_at,
        }
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.state_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            tmp.replace(self.state_path)
        except OSError as exc:  # pragma: no cover - disk full / read-only
            logger.warning("could not save NOAA state: %s", exc)

    # ------------------------------------------------------------------ location
    def set_point(self, latitude: float, longitude: float, label: str | None = None) -> bool:
        """Change the location. Returns True if it actually moved."""
        if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
            raise ValueError("latitude/longitude out of range")
        point = (_round(latitude), _round(longitude))
        moved = point != self.point
        self.point = point
        self.label = (label or "").strip()[:60] or None
        if moved:
            # Alerts and conditions for the old place no longer apply.
            self.current_ids = set()
            self.polled = False
            self.observation = None
            self.station = None
        self._save()
        self._kick.set()
        return moved

    def is_current(self, identifier: str | None) -> bool:
        """Should a NOAA alert with this id still be shown?"""
        if not self.polled:
            return True
        return bool(identifier) and identifier in self.current_ids

    # ------------------------------------------------------------------ NWS calls
    async def _get(self, client: httpx.AsyncClient, url: str) -> dict:
        response = await client.get(url)
        if response.status_code >= 400:
            raise RuntimeError(f"NWS HTTP {response.status_code} for {url}: {response.text[:200]}")
        return response.json()

    async def fetch_alerts(self, client: httpx.AsyncClient) -> list[dict]:
        lat, lon = self.point
        data = await self._get(client, f"{self.base_url}/alerts/active?point={lat},{lon}")
        alerts = []
        for feature in data.get("features") or []:
            props = (feature or {}).get("properties") or {}
            if props.get("status") != "Actual" or props.get("messageType") == "Cancel":
                continue
            if props.get("id"):
                alerts.append(props)
        return alerts

    async def fetch_observation(self, client: httpx.AsyncClient) -> dict | None:
        lat, lon = self.point
        if not self.station:
            point = await self._get(client, f"{self.base_url}/points/{lat},{lon}")
            stations_url = (point.get("properties") or {}).get("observationStations")
            if not stations_url:
                return None
            stations = await self._get(client, stations_url)
            features = stations.get("features") or []
            if not features:
                return None
            props = features[0].get("properties") or {}
            self.station = {"id": props.get("stationIdentifier"), "name": props.get("name")}
        latest = await self._get(
            client, f"{self.base_url}/stations/{self.station['id']}/observations/latest"
        )
        p = latest.get("properties") or {}
        temp_c = _value(p.get("temperature"))
        feels_c = _value(p.get("heatIndex"))
        if feels_c is None:
            feels_c = _value(p.get("windChill"))
        return {
            "station": self.station,
            "observed_at": p.get("timestamp"),
            "text": p.get("textDescription") or None,
            "temperature_f": _c_to_f(temp_c),
            "feels_like_f": _c_to_f(feels_c if feels_c is not None else temp_c),
            "humidity_pct": round(_value(p.get("relativeHumidity")), 0)
            if isinstance(_value(p.get("relativeHumidity")), (int, float))
            else None,
            "wind_mph": _kmh_to_mph(_value(p.get("windSpeed"))),
            "source": "NOAA National Weather Service",
        }

    # ------------------------------------------------------------------ polling
    async def poll_once(self) -> dict:
        if self.point is None:
            return {"status": "skipped", "reason": "no location set"}
        if not self.monitor.is_online():
            return {"status": "skipped", "reason": "offline; showing last NOAA data"}

        async with self._lock:
            headers = {"User-Agent": self.user_agent, "Accept": "application/geo+json"}
            new = 0
            try:
                async with httpx.AsyncClient(
                    timeout=httpx.Timeout(self.timeout), headers=headers, follow_redirects=True
                ) as client:
                    alerts = await self.fetch_alerts(client)
                    for props in alerts:
                        if await self.store.has_alert(props["id"]):
                            continue
                        await self.store.create(
                            "alert",
                            {
                                "alert": to_pipeline_alert(props),
                                "languages": ["en", "es"],
                                "audience": "public",
                                "source": SOURCE,
                            },
                        )
                        new += 1
                    self.current_ids = {props["id"] for props in alerts}
                    self.polled = True
                    try:
                        observation = await self.fetch_observation(client)
                        if observation:
                            self.observation = observation
                    except (httpx.HTTPError, RuntimeError, KeyError, ValueError) as exc:
                        logger.warning("NOAA observation unavailable: %s", exc)
            except (httpx.HTTPError, RuntimeError, ValueError) as exc:
                self.last_error = f"{type(exc).__name__}: {exc}"
                logger.warning("NOAA poll failed: %s", self.last_error)
                return {"status": "error", "error": self.last_error}

            if new:
                self.ingested += new
                self.wake()
            self.last_error = None
            self.last_poll_at = datetime.now(timezone.utc).isoformat()
            self._save()
            return {"status": "ok", "active": len(self.current_ids), "new": new}

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                await self.poll_once()
            except Exception:  # pragma: no cover - a poll must never kill the loop
                logger.exception("NOAA poll crashed")
            self._kick.clear()
            stop = asyncio.create_task(self._stop.wait())
            kick = asyncio.create_task(self._kick.wait())
            await asyncio.wait({stop, kick}, timeout=self.interval, return_when=asyncio.FIRST_COMPLETED)
            for task in (stop, kick):
                task.cancel()

    async def start(self) -> None:
        if self._task is None or self._task.done():
            self._stop.clear()
            self._task = asyncio.create_task(self._run(), name="noaa-feed")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None

    def snapshot(self) -> dict:
        return {
            "source": "NOAA National Weather Service (api.weather.gov)",
            "point": {"latitude": self.point[0], "longitude": self.point[1]} if self.point else None,
            "label": self.label,
            "active_alerts": len(self.current_ids),
            "polled": self.polled,
            "last_poll_at": self.last_poll_at,
            "last_error": self.last_error,
            "ingested_total": self.ingested,
            "interval_seconds": self.interval,
            "observation": self.observation,
        }
