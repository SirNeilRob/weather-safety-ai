"""NOAA / National Weather Service feed.

Pulls two things for every city on the watch list (up to ``max_cities``) from
``api.weather.gov`` (free, no key, but NWS requires an identifying User-Agent):

* **Active alerts** (``/alerts/active?point=lat,lon``). Each new alert is queued
  through the normal Weather -> Safety -> Communication pipeline exactly once.
* **Current conditions** from the nearest observation station
  (``/points`` -> ``observationStations`` -> ``observations/latest``).

NWS replaces alerts by issuing new messages (updates, cancellations) and drops
them from the active list when they end, so the active list itself is treated
as the truth: the ids from the last successful poll decide which NOAA alerts
the dashboard shows. One alert that covers two watched cities is stored once and
tagged with both. The watch list, each city's alert ids and its last observation
are saved next to the database, so after a reboot with no internet the board
still shows the last known official picture.
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




class WatchlistError(Exception):
    """A watch list change that the caller must be told about (HTTP status + message)."""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail)
        self.status = status
        self.detail = detail


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _public(city: dict) -> dict:
    return {
        "key": city["key"],
        "zip": city.get("zip"),
        "lat": city["lat"],
        "lon": city["lon"],
        "name": city.get("name"),
        "added_at": city.get("added_at"),
        "active_alerts": len(city.get("alert_ids") or []),
        "polled": bool(city.get("polled")),
        "last_poll_at": city.get("last_poll_at"),
        "last_error": city.get("last_error"),
        "observation": city.get("observation"),
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
        max_cities: int = 5,
        timeout: float = 15.0,
        base_url: str = NWS_BASE,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.store = store
        self.monitor = monitor
        self.wake = wake
        self.state_path = Path(state_path)
        self.user_agent = user_agent
        self.interval = max(15.0, float(interval))
        self.max_cities = int(max_cities)
        self.timeout = float(timeout)
        self.base_url = base_url.rstrip("/")
        self.transport = transport  # tests swap in an httpx.MockTransport

        self.cities: list[dict] = []
        self.last_poll_at: str | None = None
        self.last_error: str | None = None
        self.ingested = 0

        self._task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._kick = asyncio.Event()
        self._lock = asyncio.Lock()

        self._load()

    # ------------------------------------------------------------------ state file
    def _load(self) -> None:
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        # Files from the single-location version have no "cities" key; that old
        # location is dropped and the watch list starts empty.
        cities = data.get("cities")
        if isinstance(cities, list):
            self.cities = [c for c in cities if isinstance(c, dict) and c.get("key")][: self.max_cities]
        self.last_poll_at = data.get("last_poll_at")

    def _save(self) -> None:
        data = {"cities": self.cities, "last_poll_at": self.last_poll_at}
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.state_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            tmp.replace(self.state_path)
        except OSError as exc:  # pragma: no cover - disk full / read-only
            logger.warning("could not save NOAA state: %s", exc)

    # ------------------------------------------------------------------ watch list
    def _find(self, key: str) -> dict | None:
        return next((c for c in self.cities if c["key"] == key), None)

    def _check_room(self) -> None:
        if len(self.cities) >= self.max_cities:
            raise WatchlistError(409, f"Max {self.max_cities} cities")

    def _append(self, city: dict) -> dict:
        city.update(alert_ids=[], polled=False, observation=None, station=None,
                    last_poll_at=None, last_error=None, added_at=_now())
        self.cities.append(city)
        self._save()
        self._kick.set()
        return city

    async def add_zip(self, raw_zip: str) -> dict:
        from . import zipcodes

        zip_code = zipcodes.normalize(raw_zip)
        if zip_code is None:
            raise WatchlistError(422, "ZIP must be 5 digits")
        if not zipcodes.is_florida(zip_code):
            raise WatchlistError(422, "Florida ZIPs only for now")
        point = zipcodes.lookup(zip_code)
        if point is None:
            raise WatchlistError(404, "ZIP not found")
        if self._find(zip_code):
            raise WatchlistError(409, f"ZIP {zip_code} is already on the watch list")
        self._check_room()
        city = self._append({"key": zip_code, "zip": zip_code, "lat": _round(point[0]),
                             "lon": _round(point[1]), "name": None})
        if self.monitor.is_online():
            # Best effort: the poll loop retries until NWS gives a name.
            try:
                async with self._client() as client:
                    await self._resolve_point(client, city)
                self._save()
            except (httpx.HTTPError, RuntimeError, ValueError) as exc:
                logger.info("NWS name lookup for %s failed: %s", zip_code, exc)
        return _public(city)

    def add_point(self, latitude: float, longitude: float, name: str) -> dict:
        if not (-90 <= latitude <= 90 and -180 <= longitude <= 180):
            raise WatchlistError(422, "latitude/longitude out of range")
        name = (name or "").strip()[:80]
        if not name:
            raise WatchlistError(422, "name is required")
        lat, lon = _round(latitude), _round(longitude)
        if any(c["lat"] == lat and c["lon"] == lon for c in self.cities):
            raise WatchlistError(409, "That point is already on the watch list")
        self._check_room()
        used = {c["key"] for c in self.cities}
        key = next(f"fl{n}" for n in range(1, self.max_cities + 2) if f"fl{n}" not in used)
        return _public(self._append({"key": key, "zip": None, "lat": lat, "lon": lon, "name": name}))

    def remove(self, key: str) -> bool:
        city = self._find(key)
        if city is None:
            return False
        self.cities.remove(city)
        self._save()
        return True

    def watchlist(self) -> list[dict]:
        return [_public(c) for c in self.cities]

    def is_current(self, identifier: str | None) -> bool:
        """Should a NOAA alert with this id still be shown? Only if a watched city has it."""
        return bool(identifier) and any(identifier in (c.get("alert_ids") or []) for c in self.cities)

    def cities_for(self, identifier: str | None) -> list[dict]:
        if not identifier:
            return []
        return [{"key": c["key"], "zip": c.get("zip"), "name": c.get("name")}
                for c in self.cities if identifier in (c.get("alert_ids") or [])]

    # ------------------------------------------------------------------ NWS calls
    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            timeout=httpx.Timeout(self.timeout),
            headers={"User-Agent": self.user_agent, "Accept": "application/geo+json"},
            follow_redirects=True,
            transport=self.transport,
        )

    async def _get(self, client: httpx.AsyncClient, url: str) -> dict:
        response = await client.get(url)
        if response.status_code >= 400:
            raise RuntimeError(f"NWS HTTP {response.status_code} for {url}: {response.text[:200]}")
        return response.json()

    async def _resolve_point(self, client: httpx.AsyncClient, city: dict) -> None:
        """Fill the city name (ZIP cities) and nearest station from NWS /points."""
        if city.get("station") and city.get("name"):
            return
        point = await self._get(client, f"{self.base_url}/points/{city['lat']},{city['lon']}")
        props = point.get("properties") or {}
        rel = ((props.get("relativeLocation") or {}).get("properties")) or {}
        if not city.get("name") and rel.get("city") and rel.get("state"):
            city["name"] = f"{rel['city']}, {rel['state']}"
        if not city.get("station") and props.get("observationStations"):
            stations = await self._get(client, props["observationStations"])
            features = stations.get("features") or []
            if features:
                sp = features[0].get("properties") or {}
                city["station"] = {"id": sp.get("stationIdentifier"), "name": sp.get("name")}

    async def fetch_alerts(self, client: httpx.AsyncClient, city: dict) -> list[dict]:
        data = await self._get(client, f"{self.base_url}/alerts/active?point={city['lat']},{city['lon']}")
        alerts = []
        for feature in data.get("features") or []:
            props = (feature or {}).get("properties") or {}
            if props.get("status") != "Actual" or props.get("messageType") == "Cancel":
                continue
            if props.get("id"):
                alerts.append(props)
        return alerts

    async def fetch_observation(self, client: httpx.AsyncClient, city: dict) -> dict | None:
        await self._resolve_point(client, city)
        station = city.get("station")
        if not station or not station.get("id"):
            return None
        latest = await self._get(client, f"{self.base_url}/stations/{station['id']}/observations/latest")
        p = latest.get("properties") or {}
        temp_c = _value(p.get("temperature"))
        feels_c = _value(p.get("heatIndex"))
        if feels_c is None:
            feels_c = _value(p.get("windChill"))
        humidity = _value(p.get("relativeHumidity"))
        return {
            "station": station,
            "observed_at": p.get("timestamp"),
            "text": p.get("textDescription") or None,
            "temperature_f": _c_to_f(temp_c),
            "feels_like_f": _c_to_f(feels_c if feels_c is not None else temp_c),
            "humidity_pct": round(humidity, 0) if isinstance(humidity, (int, float)) else None,
            "wind_mph": _kmh_to_mph(_value(p.get("windSpeed"))),
            "source": "NOAA National Weather Service",
        }

    # ------------------------------------------------------------------ polling
    async def _ingest(self, props: dict) -> bool:
        if await self.store.has_alert(props["id"]):
            return False
        await self.store.create(
            "alert",
            {"alert": to_pipeline_alert(props), "languages": ["en", "es"],
             "audience": "public", "source": SOURCE},
        )
        return True

    async def poll_once(self) -> dict:
        if not self.cities:
            return {"status": "skipped", "reason": "watch list is empty"}
        if not self.monitor.is_online():
            return {"status": "skipped", "reason": "offline; showing last NOAA data"}

        async with self._lock:
            new = 0
            errors: list[str] = []
            async with self._client() as client:
                for city in list(self.cities):
                    try:
                        alerts = await self.fetch_alerts(client, city)
                        for props in alerts:
                            if await self._ingest(props):
                                new += 1
                        city["alert_ids"] = sorted({props["id"] for props in alerts})
                        city["polled"] = True
                        city["last_poll_at"] = _now()
                        city["last_error"] = None
                    except (httpx.HTTPError, RuntimeError, ValueError) as exc:
                        city["last_error"] = f"{type(exc).__name__}: {exc}"
                        errors.append(f"{city['key']}: {city['last_error']}")
                        continue
                    try:
                        observation = await self.fetch_observation(client, city)
                        if observation:
                            city["observation"] = observation
                    except (httpx.HTTPError, RuntimeError, KeyError, ValueError) as exc:
                        logger.warning("NOAA observation for %s unavailable: %s", city["key"], exc)

            if new:
                self.ingested += new
                self.wake()
            self.last_error = "; ".join(errors) or None
            if len(errors) < len(self.cities) or not self.cities:
                self.last_poll_at = _now()
            self._save()
            if errors:
                logger.warning("NOAA poll errors: %s", self.last_error)
            active = {i for c in self.cities for i in (c.get("alert_ids") or [])}
            return {"status": "error" if len(errors) == len(self.cities) else "ok",
                    "cities": len(self.cities), "active": len(active), "new": new,
                    "errors": errors}

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
        active = {i for c in self.cities for i in (c.get("alert_ids") or [])}
        return {
            "source": "NOAA National Weather Service (api.weather.gov)",
            "cities": self.watchlist(),
            "max_cities": self.max_cities,
            "active_alerts": len(active),
            "last_poll_at": self.last_poll_at,
            "last_error": self.last_error,
            "ingested_total": self.ingested,
            "interval_seconds": self.interval,
        }
