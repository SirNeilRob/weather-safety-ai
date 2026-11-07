# ResQ integration log

## 2026-11-07 12:50 EST — branch city-watchlist

**Changed**
- `app/core/noaa.py`: one NOAA location → watch list of up to 5 cities (zip, lat, lon, name, added_at), saved in `data/noaa_state.json`. Alerts polled per city every 60 s; one alert in two cities = one card listing both. One observation per city. City name from NWS `/points` relativeLocation, else "ZIP 12345".
- `app/api/routes.py`: `/v1/alerts/active` items carry `cities`; health reports city count.
- `app/config.py`: `NOAA_LAT`/`NOAA_LON` replaced by `WATCHLIST_MAX` (5); api.weather.gov is always probed first (a stale exported `PROBE_URLS` had hidden it).
- `Front End/index.html`: watch list panel (Florida ZIP + Add, ✕ remove, empty state), Browse Florida (live `alerts/active?area=FL`, Add to watch list via point-in-polygon, named by areaDesc), "City:" line on cards, per-city weather, Internet chip says "weather.gov reachable", duplicate first "Do now" bullet removed.
- `requirements.txt`: added requests, python-dotenv. `test/test_swarms_connection.py`: loads `.env` from the repo root.
- `scripts/smoke_test.sh`: check 6 adds a ZIP, lists it, removes it.
- Docs: contract, README, Front End README, TEAM_SPLIT updated for the watch list.

**Added**
- `app/data/zcta.csv.gz`: Census 2024 Gazetteer ZCTA (zip, lat, lon), public domain. `app/core/zipcodes.py`: offline lookup; Florida ZIPs only for now (32/33/34).
- Endpoints: `GET/POST /v1/watchlist`, `POST /v1/watchlist/point`, `DELETE /v1/watchlist/{key}`.
- `tests/test_watchlist.py`: valid ZIP, invalid ZIP, non-Florida ZIP, 6th city rejected, remove, restart persistence, same alert in 2 cities = 1 card, empty list.

**Removed**
- `POST /v1/noaa/location`, the NOAA Location panel (GPS, coordinates), "Find live alert".
- All Arizona data: old single-location state file deleted; 3 Maricopa, AZ jobs deleted from the local DB (5 → 2 jobs, 0 non-Florida); test text moved to Miami-Dade County.

Tests: 110 passed.
