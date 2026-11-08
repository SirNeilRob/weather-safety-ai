"""ResQ quake sensor: Linux half of the App Lab app.

The sketch measures one shake as (peak g, duration s) and sends it with
``Bridge.notify("shake_event", peak, duration)``. This file turns that pair into
an alert using the team's ``sensor_bridge`` module unchanged: same noise floor,
knock filter, cooldown, PGA -> MMI -> severity mapping, CAP alert shape and
on-disk spool. Only the input differs: Bridge instead of a serial port.

    sketch --Bridge--> on_shake() --queue--> worker: Gate -> build_alert -> POST /v1/alerts
                                                            (spooled if the backend is down)

The App Lab Python runs in a container on its own Docker network, so the
backend on the board is reached through the host alias App Lab provides
(``msgpack-rpc-router`` -> host gateway), not 127.0.0.1.
"""

from __future__ import annotations

import os
import queue
import threading
import time
from pathlib import Path

from arduino.app_utils import App, Bridge

import sensor_bridge as sb  # copied next to this file at deploy time (scripts/sensor_bridge.py)

BACKEND = os.environ.get("RESQ_BACKEND", "http://msgpack-rpc-router:8000").rstrip("/")
ALERT_URL = f"{BACKEND}/v1/alerts"
NODE = os.environ.get("RESQ_NODE", "resq-q1")
LOCATION = os.environ.get("RESQ_LOCATION", "ResQ node - Hackathon venue")
WAIT_SECONDS = 25.0  # how long the backend may take to run the pipeline (MoA included)
HTTP_TIMEOUT = 60.0
RETRY_SPOOL_EVERY = 30.0

gate = sb.Gate()  # defaults: 0.03 g floor, 1.0 s knock gate, 120 s cooldown
spool = sb.Spool(Path(__file__).resolve().parent.parent / "data" / "bridge_spool.jsonl")
events: "queue.Queue[sb.Shake]" = queue.Queue()


def log(message: str) -> None:
    print(f"[quake {time.strftime('%H:%M:%S')}] {message}", flush=True)


def send(alert: dict) -> bool:
    try:
        sb.post_alert(ALERT_URL, alert, wait_seconds=WAIT_SECONDS, timeout=HTTP_TIMEOUT)
        return True
    except Exception as exc:  # network down, backend restarting, bad reply
        log(f"delivery failed: {exc}")
        return False


# ------------------------------------------------------------------ Bridge handlers
# Handlers return immediately; the HTTP call happens on the worker thread so a
# slow backend never stalls the Bridge.


def on_shake(peak_g: float, duration_s: float) -> None:
    events.put(sb.Shake(peak_g=float(peak_g), duration_s=float(duration_s), node=NODE))


def on_status(code: int, value: int) -> None:
    if code == 1:
        log(f"ADXL345 found at I2C address 0x{int(value):02X}; measuring")
    elif code == 2:
        log("ADXL345 NOT found on I2C (check 3.3V, GND, SDA, SCL); retrying")
    elif code == 3:
        log(f"heartbeat: sensor running, {int(value)} shake event(s) measured so far")


Bridge.provide("shake_event", on_shake)
Bridge.provide("sensor_status", on_status)


# ------------------------------------------------------------------ worker
def handle(shake: sb.Shake) -> None:
    reason = gate.rejection(shake)
    if reason is not None:
        log(f"skipped {shake.peak_g:.3f} g / {shake.duration_s:.2f} s: {reason}")
        return

    alert = sb.build_alert(shake, location=LOCATION, sender="ResQ on-device sensor",
                           event="Earthquake Warning")
    log(f"QUAKE peak {shake.peak_g:.2f} g / {shake.duration_s:.1f} s "
        f"= MMI {shake.mmi_int} -> {shake.severity}; sending")
    gate.record(shake)  # one quake, one alert, even if delivery is slow

    spool.drain(send)
    if send(alert):
        log("alert delivered to the pipeline")
    else:
        spool.add(alert)
        log(f"backend unreachable; alert saved to {spool.path} and will be resent")


def worker() -> None:
    last_retry = 0.0
    while True:
        try:
            shake = events.get(timeout=5)
        except queue.Empty:
            if time.monotonic() - last_retry > RETRY_SPOOL_EVERY and spool.pending():
                last_retry = time.monotonic()
                sent = spool.drain(send)
                if sent:
                    log(f"resent {sent} saved alert(s)")
            continue
        try:
            handle(shake)
        except Exception as exc:  # never let one bad event kill the worker
            log(f"error handling event: {exc}")


log(f"starting; alerts go to {ALERT_URL}")
state = sb.probe_health(ALERT_URL)
log(f"backend link: {state}" if state else "backend not answering yet; alerts will be saved and resent")
threading.Thread(target=worker, daemon=True).start()

App.run()
