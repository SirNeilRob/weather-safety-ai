"""Live drill with a real NWS alert: online -> forced offline -> back online -> reconcile.

    python3 scripts/offline_drill.py --base-url http://127.0.0.1:8000

Fetches one currently active alert from api.weather.gov (so the machine running
the drill needs internet for step 2), then pushes that same real alert through
the backend online and with the link forced down. It checks that the job is
still processed offline, that the result says which engine produced it, and that
the backend catches up afterwards.

Note: the alerts submitted here are real NWS alerts, but they are not tied to the
backend's NOAA location, so they show on the dashboard until they expire.

Exit code 0 means every check passed.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from app.core.noaa import to_pipeline_alert  # noqa: E402

NWS_ACTIVE = "https://api.weather.gov/alerts/active?status=actual&message_type=alert"
USER_AGENT = "ResQ-weather-safety/0.1 (+https://github.com/SirNeilRob/weather-safety-ai)"
NWS_RANK = {"unknown": 0, "minor": 1, "moderate": 2, "severe": 3, "extreme": 4}

CHECKS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    CHECKS.append((name, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" - {detail}" if detail else ""))


def live_nws_alert() -> dict | None:
    """The most severe currently active NWS alert, as NWS properties."""
    response = httpx.get(
        NWS_ACTIVE,
        headers={"User-Agent": USER_AGENT, "Accept": "application/geo+json"},
        timeout=20.0,
    )
    response.raise_for_status()
    alerts = [f["properties"] for f in response.json().get("features", []) if f.get("properties")]
    alerts = [p for p in alerts if p.get("id") and p.get("event")]
    if not alerts:
        return None
    return max(alerts, key=lambda p: NWS_RANK.get(str(p.get("severity", "")).lower(), 0))


def submit(client, alert: dict) -> dict:
    response = client.post("/v1/alerts", json={"alert": alert, "wait_seconds": 30})
    response.raise_for_status()
    return response.json()


def run(client) -> int:
    print("\n1. Health")
    health = client.get("/v1/health").json()
    print(f"   mode={health['mode']} local={health['local']['engine']} "
          f"swarm_configured={health['swarm']['configured']}")
    check("health endpoint answers", health.get("ok") is True)
    check("service reports a connectivity state",
          health["connectivity"]["state"] in ("online", "offline"))

    print("\n2. Fetch a real active alert from api.weather.gov")
    try:
        props = live_nws_alert()
    except httpx.HTTPError as exc:
        props = None
        print(f"   NWS error: {type(exc).__name__}: {exc}")
    check("NWS returned an active alert", props is not None)
    if props is None:
        return summary()
    print(f"   {props['id']} | {props['event']} | {props.get('severity')} | "
          f"{(props.get('areaDesc') or '')[:70]}")
    alert = to_pipeline_alert(props)
    nws_rank = NWS_RANK.get(str(props.get("severity", "")).lower(), 0)

    print("\n3. Same alert while the link is up")
    online = submit(client, alert)
    result = online.get("result") or {}
    print(f"   status={online['status']} job_engine={online['engine']} "
          f"pipeline_engine={result.get('pipeline_engine')}")
    check("online alert completed", online["status"] == "done")

    print("\n4. Force offline (no cable pulling required)")
    client.post("/v1/demo/offline", json={"enabled": True}).raise_for_status()
    try:
        offline_health = client.get("/v1/health").json()
        check("backend reports offline", offline_health["mode"] == "offline",
              f"state={offline_health['connectivity']['state']}")

        offline = submit(client, alert)
        result = offline.get("result") or {}
        classification = result.get("classification") or {}
        safety = result.get("safety") or {}
        agents = [step.get("agent") for step in result.get("trace", [])]
        print(f"   status={offline['status']} job_engine={offline['engine']} "
              f"pipeline_engine={result.get('pipeline_engine')} sync={offline['sync_state']}")
        print(f"   hazard={classification.get('hazard')} severity={classification.get('severity')}")
        check("offline alert still completed", offline["status"] == "done")
        check("offline alert ran on the local engine", offline["engine"] == "local",
              f"engine={offline['engine']}")
        check("result says it was produced locally", result.get("pipeline_engine") == "local",
              f"pipeline_engine={result.get('pipeline_engine')}")
        check("agent handoff chain is intact",
              agents == ["weather", "safety", "communication"], str(agents))
        check("severity not downgraded below NWS",
              (classification.get("severity_rank") or 0) >= nws_rank,
              f"nws={props.get('severity')} resq={classification.get('severity')}")
        check("safety advice is bilingual",
              bool((safety.get("first_action") or {}).get("en"))
              and bool((safety.get("first_action") or {}).get("es")))
    finally:
        print("\n5. Restore connectivity")
        client.post("/v1/demo/offline", json={"enabled": False}).raise_for_status()
    restored = client.get("/v1/health").json()
    check("forced-offline switch released", restored["connectivity"]["forced"] is False)

    print("\n6. Reconcile the offline work")
    sync = client.post("/v1/sync/run").json()
    print(f"   {json.dumps(sync)}")
    check("sync pass answered", sync.get("status") in ("ok", "idle", "disabled", "skipped"),
          json.dumps(sync))

    print("\n7. Queue summary")
    stats = client.get("/v1/health").json()["queue"]
    print(f"   {json.dumps(stats)}")
    check("no job was lost", stats["failed"] == 0, json.dumps(stats))
    return summary()


def summary() -> int:
    failed = [item for item in CHECKS if not item[1]]
    print(f"\n{len(CHECKS) - len(failed)}/{len(CHECKS)} checks passed")
    if failed:
        print("failed: " + ", ".join(name for name, _, _ in failed))
        return 1
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    args = parser.parse_args()
    with httpx.Client(base_url=args.base_url.rstrip("/"), timeout=60.0) as client:
        return run(client)


if __name__ == "__main__":
    sys.exit(main())
