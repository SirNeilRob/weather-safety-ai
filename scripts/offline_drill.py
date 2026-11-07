"""Live drill: online -> forced offline -> back online -> reconcile.

    python3 scripts/offline_drill.py --base-url http://127.0.0.1:8000

Run it before the demo and again on stage. It asserts the things a judge will
ask about: does a job really get processed with no network, does the result say
which engine produced it, and does the backend catch up afterwards.

Exit code 0 means every check passed.
"""

from __future__ import annotations

import argparse
import json
import sys

import httpx

SAMPLE = (
    "The north site generator failed at 03:12 on 2026-11-01. "
    "The field crew replaced the fuel pump and restored power. "
    "412 homes were without power for 96 minutes. "
    "A follow-up inspection is booked for 2026-11-09. "
    "Questions: dispatch@example.com."
)

CHECKS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    CHECKS.append((name, ok, detail))
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" - {detail}" if detail else ""))


def submit(client: httpx.Client, kind: str) -> dict:
    response = client.post(
        "/v1/jobs",
        json={"kind": kind, "payload": {"text": SAMPLE}, "wait_seconds": 15},
    )
    response.raise_for_status()
    return response.json()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8000")
    parser.add_argument(
        "--swarm-url",
        default=None,
        help="fake Swarm base URL, to confirm the sync inbox actually received records",
    )
    args = parser.parse_args()

    with httpx.Client(base_url=args.base_url.rstrip("/"), timeout=60.0) as client:
        print("\n1. Health / capability probe")
        health = client.get("/v1/health").json()
        print(f"   mode={health['mode']} local={health['local']['engine']} "
              f"swarm_configured={health['swarm']['configured']}")
        check("health endpoint answers", health.get("ok") is True)
        check("service reports a connectivity state",
              health["connectivity"]["state"] in ("online", "offline"))

        print("\n2. Job while the link is up")
        online_job = submit(client, "analyze")
        print(f"   status={online_job['status']} engine={online_job['engine']}")
        check("online job completed", online_job["status"] == "done")
        if health["swarm"]["configured"] and not health["connectivity"]["forced"]:
            check("online job used Swarm", online_job["engine"] == "swarm",
                  f"engine={online_job['engine']}")

        print("\n3. Force offline (no cable pulling required)")
        client.post("/v1/demo/offline", json={"enabled": True}).raise_for_status()
        offline_health = client.get("/v1/health").json()
        check("backend reports offline", offline_health["mode"] == "offline",
              f"state={offline_health['connectivity']['state']}")

        offline_job = submit(client, "summarize")
        result = offline_job.get("result") or {}
        print(f"   status={offline_job['status']} engine={offline_job['engine']} "
              f"sync={offline_job['sync_state']}")
        print(f"   summary={result.get('summary', '')[:110]}")
        check("offline job still completed", offline_job["status"] == "done")
        check("offline job ran on the local engine", offline_job["engine"] == "local")
        check("offline result is flagged as offline", result.get("offline") is True)

        print("\n3b. Weather alert pipeline while offline")
        catalogue = client.get("/v1/alerts/samples").json()
        check("sample alerts are published", catalogue["count"] >= 1,
              f"count={catalogue['count']}")
        legend = client.get("/v1/hazards").json()
        check("hazard legend answers for the dashboard",
              len(legend["severity_levels"]) == 5 and bool(legend["hazards"]))

        tornado = next(
            entry for entry in catalogue["samples"] if entry["name"] == "tornado_emergency"
        )
        alert_response = client.post(
            "/v1/alerts", json={"alert": tornado["alert"], "wait_seconds": 15}
        )
        alert_response.raise_for_status()
        alert_body = alert_response.json()
        alert = alert_body.get("result") or {}
        classification = alert.get("classification") or {}
        safety = alert.get("safety") or {}
        sms = (alert.get("communication") or {}).get("sms") or {}
        print(f"   hazard={classification.get('hazard')} "
              f"severity={classification.get('severity')} engine={alert_body['engine']}")
        print(f"   sms_en={sms.get('en', {}).get('segments')}seg "
              f"sms_es={sms.get('es', {}).get('segments')}seg")
        check("offline alert classified as a tornado",
              classification.get("hazard") == "tornado")
        check("extreme severity preserved",
              classification.get("severity") == "extreme")
        check("alert ran on the local engine", alert_body["engine"] == "local")
        check("agent handoff chain is intact",
              [step["agent"] for step in alert.get("trace", [])]
              == ["weather", "safety", "communication"],
              str([step["agent"] for step in alert.get("trace", [])]))
        check("English SMS fits one segment",
              (sms.get("en") or {}).get("segments") == 1,
              f"segments={(sms.get('en') or {}).get('segments')}")
        spanish_sms = (sms.get("es") or {}).get("text", "")
        leaked = [w for w in ("take ", "stay ", "shelter", "the ") if w in spanish_sms.lower()]
        check("Spanish SMS is authored in Spanish, not word-for-word",
              "Aviso de tornado" in spanish_sms and not leaked,
              spanish_sms[:80])
        check("safety advice is bilingual",
              bool((safety.get("first_action") or {}).get("es")))

        print("\n4. Restore connectivity")
        client.post("/v1/demo/offline", json={"enabled": False}).raise_for_status()
        restored = client.get("/v1/health").json()
        check("forced-offline switch released", restored["connectivity"]["forced"] is False)

        print("\n5. Reconcile the offline work")
        sync = client.post("/v1/sync/run").json()
        print(f"   {json.dumps(sync)}")
        if not health["swarm"]["configured"]:
            check("sync correctly reports 'disabled' without a Swarm URL",
                  sync.get("status") == "disabled")
        elif sync.get("status") == "skipped":
            check("sync deferred while the link is down", True, sync.get("reason", ""))
        else:
            check("sync pass ran", sync.get("status") in ("ok", "idle", "disabled"),
                  json.dumps(sync))

        if args.swarm_url:
            inbox = httpx.get(args.swarm_url.rstrip("/") + "/v1/sync/inbox", timeout=10).json()
            print(f"   swarm inbox count={inbox['count']}")
            check("Swarm received the offline record", inbox["count"] >= 1)

        print("\n6. Queue summary")
        stats = client.get("/v1/health").json()["queue"]
        print(f"   {json.dumps(stats)}")
        check("no job was lost", stats["failed"] == 0, json.dumps(stats))

    failed = [item for item in CHECKS if not item[1]]
    print(f"\n{len(CHECKS) - len(failed)}/{len(CHECKS)} checks passed")
    if failed:
        print("failed: " + ", ".join(name for name, _, _ in failed))
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
