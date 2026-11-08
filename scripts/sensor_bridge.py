#!/usr/bin/env python3
"""Turn one accelerometer reading into a CAP-shaped alert for the pipeline.

Part three of the ResQ chain. The microcontroller measures the strongest ground
acceleration during a shake and how long the shaking lasted; this script turns
that pair of numbers into the same alert shape a national weather service would
send, posts it to ``POST /v1/alerts``, and lets the existing pipeline do the
work it already does: classify the hazard, attach the bilingual playbook, and
write the SMS / push / bulletin / voice copy.

    ESP32 --serial--> sensor_bridge.py --HTTP--> /v1/alerts --> weather
                                                             -> safety
                                                             -> communication

The mapping from raw acceleration to a severity word is not a guess. It uses
the PGA-to-intensity relation of Worden et al. (2012),

    MMI = 3.66 * log10(PGA[cm/s^2]) - 1.66

which reproduces the familiar Wald et al. (1999) shaking table: 0.05 g is
roughly MMI V, 0.1 g is VI, 0.2 g is VII, 0.4 g is VIII. The severity word the
pipeline receives is derived from that intensity, so the sensor and the alert
vocabulary cannot drift apart.

Input, one event per line, from a serial port or stdin:

    PEAK=0.21 DUR=2.4           g and seconds
    PEAK=210mg DUR=2400ms       units honoured
    0.21,2.4                    bare CSV, peak first
    {"peak_g": 0.21, "duration_s": 2.4}

Usage:

    # a node on USB serial
    python3 scripts/sensor_bridge.py --port /dev/ttyACM0

    # replay a capture, no hardware
    python3 scripts/sensor_bridge.py --once < shake.log

    # show the alert without posting it
    echo 'PEAK=0.21 DUR=2.4' | python3 scripts/sensor_bridge.py --once --dry-run

Two gates keep a dropped box from paging the whole venue: a reading must clear
a floor peak acceleration *and* last longer than ``--min-duration``. A dropped
board produces a huge peak over a few tens of milliseconds; a real MMI VII
quake shakes for seconds. Alerts are also rate-limited by ``--cooldown`` so one
earthquake produces one message, with an override when the shaking gets worse.

Stdlib only, on purpose: this runs on a field laptop next to a microcontroller,
not necessarily inside the backend virtualenv.
"""

from __future__ import annotations

import argparse
import json
import math
import os
import select
import stat
import sys
import termios
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_URL = "http://127.0.0.1:8000/v1/alerts"

#: Sensor floor. Below MMI V a MEMS reading is as likely to be a door slam as
#: an earthquake, so it is measured but not alerted on.
MIN_PEAK_G = 0.03

#: Shaking shorter than this is a knock, not a quake.
MIN_DURATION_S = 1.0

#: One earthquake, one alert. A worse reading still gets through.
COOLDOWN_S = 120.0

#: Anything past these is a sensor fault, not a seismogram.
MAX_PEAK_G = 20.0
MAX_DURATION_S = 600.0

#: Intensity at which an alert is worth sending (Wald et al. 1999: IV is
#: "light", widely felt indoors; V is "moderate", felt by nearly everyone).
ALERT_MMI = 5

SEVERITY_RANK = {"minor": 1, "moderate": 2, "severe": 3, "extreme": 4}

HEADLINES = {
    "minor": "Shaking detected by ResQ sensor",
    "moderate": "Moderate shaking detected by ResQ sensor",
    "severe": "Strong shaking detected by ResQ sensor",
    "extreme": "Very strong shaking detected by ResQ sensor",
}

PEAK_KEYS = {
    "peak_g": "g",
    "peak": "g",
    "pga": "g",
    "accel_g": "g",
    "acceleration_g": "g",
    "g": "g",
    "peak_mg": "mg",
    "mg": "mg",
}

DURATION_KEYS = {
    "duration_s": "s",
    "duration": "s",
    "dur": "s",
    "seconds": "s",
    "sec": "s",
    "s": "s",
    "dur_ms": "ms",
    "duration_ms": "ms",
    "millis": "ms",
    "ms": "ms",
}


class SensorParseError(ValueError):
    """A line arrived that is not a readable (peak, duration) pair."""


@dataclass(slots=True)
class Shake:
    """One measured event, normalised to g and seconds."""

    peak_g: float
    duration_s: float
    node: str = ""
    line: str = ""

    @property
    def peak_cm_s2(self) -> float:
        return self.peak_g * 980.665

    @property
    def mmi(self) -> float:
        """Estimated shaking intensity (Worden et al. 2012)."""
        pga = max(self.peak_cm_s2, 1e-6)
        return min(10.0, max(1.0, 3.66 * math.log10(pga) - 1.66))

    @property
    def mmi_int(self) -> int:
        return int(math.floor(self.mmi + 0.5))

    @property
    def severity(self) -> str | None:
        """Severity word for the pipeline, or None below the alert floor."""
        level = self.mmi_int
        if level < ALERT_MMI:
            return None
        if level == 5:
            return "minor"
        if level == 6:
            return "moderate"
        if level == 7:
            return "severe"
        return "extreme"

    @property
    def description(self) -> str:
        return f"Peak {self.peak_g:.2f} g sustained for {self.duration_s:.1f} s"


# ------------------------------------------------------------------ parsing


def _to_float(text: str, name: str) -> float:
    try:
        value = float(text.strip().replace(",", "."))
    except ValueError as exc:
        raise ValueError(f"{name} is not a number: {text!r}") from exc
    if not math.isfinite(value):
        raise ValueError(f"{name} is not a finite number: {text!r}")
    return value


def _quantity(raw: object, *, name: str, unit: str) -> float:
    """Read a value together with its unit.

    ``PEAK=210mg`` and ``peak_mg: 210`` mean the same thing; ``DUR=2400ms`` and
    ``dur_ms: 2400`` mean the same thing. A value without a suffix is read in
    the unit its key already implies, so a firmware that drops the suffix does
    not silently introduce a thousand-fold error.
    """
    text = str(raw).strip().lower()
    implicit = 1e-3 if unit in {"mg", "ms"} else 1.0
    explicit = None
    for suffix, factor in (("mg", 1e-3), ("ms", 1e-3), ("kg", 1000.0), ("g", 1.0), ("s", 1.0)):
        if text.endswith(suffix) and len(text) > len(suffix):
            text = text[: -len(suffix)]
            explicit = factor
            break
    value = _to_float(text, name)
    if value < 0:
        raise ValueError(f"{name} is negative: {raw!r}")
    return value * (implicit if explicit is None else explicit)


def _from_mapping(data: dict, node: str) -> Shake:
    lowered = {str(key).strip().lower(): value for key, value in data.items()}

    peak_key = peak_unit = None
    for key, unit in PEAK_KEYS.items():
        if key in lowered:
            peak_key, peak_unit = key, unit
            break

    duration_key = duration_unit = None
    for key, unit in DURATION_KEYS.items():
        if key in lowered:
            duration_key, duration_unit = key, unit
            break

    if peak_key is None or duration_key is None:
        raise ValueError("need both a peak and a duration")

    return Shake(
        peak_g=_quantity(lowered[peak_key], name="peak", unit=str(peak_unit)),
        duration_s=_quantity(lowered[duration_key], name="duration", unit=str(duration_unit)),
        node=str(lowered.get("node") or node or ""),
    )


def parse_line(line: str, *, node: str = "") -> Shake:
    """Parse one firmware line into a :class:`Shake`.

    Accepts JSON, ``KEY=VALUE`` pairs and bare CSV, because the firmware half of
    a hackathon bridge is usually written by someone else and the wire format
    tends to change late. All three carry the same pair of numbers.
    """
    text = line.strip()
    if not text or text.startswith("#"):
        raise ValueError("empty line")

    if text.startswith("{"):
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError(f"broken JSON: {exc.msg}") from exc
        if not isinstance(payload, dict):
            raise ValueError("JSON must be an object")
        shake = _from_mapping(payload, node)
        shake.line = text
        return shake

    if "=" in text:
        pairs: dict[str, str] = {}
        for chunk in text.replace(",", " ").split():
            if "=" not in chunk:
                continue
            key, value = chunk.split("=", 1)
            pairs[key.strip()] = value.strip()
        shake = _from_mapping(pairs, node)
        shake.line = text
        return shake

    parts = [part.strip() for part in text.replace(";", ",").split(",") if part.strip()]
    if len(parts) < 2:
        raise ValueError("expected 'peak,duration' or KEY=VALUE pairs")
    shake = Shake(
        peak_g=_quantity(parts[0], name="peak", unit="g"),
        duration_s=_quantity(parts[1], name="duration", unit="s"),
        node=node,
    )
    shake.line = text
    return shake


# ------------------------------------------------------------ alert shape


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _slug(text: str) -> str:
    return "".join(char if char.isalnum() else "-" for char in text.lower()).strip("-") or "resq"


def build_alert(shake: Shake, *, location: str, sender: str, event: str) -> dict:
    """Build the CAP-shaped alert the pipeline already knows how to read.

    Every field here is one the normaliser already understands, so the sensor
    enters the system through the same door as a feed from a weather service:
    no special case, no side channel, no privileged path.
    """
    severity = shake.severity or "minor"
    node = shake.node or "resq-node"
    return {
        "identifier": f"{_slug(node)}-{time.time_ns()}",
        "senderName": sender,
        "sent": _utc_now(),
        "status": "Actual",
        "msgType": "Alert",
        "language": "en",
        "event": event,
        "severity": severity.capitalize(),
        "urgency": "Immediate",
        "certainty": "Observed",
        "areaDesc": location,
        "headline": HEADLINES[severity],
        "description": shake.description,
        "onset": _utc_now(),
        "parameters": {
            "peakAcceleration_g": round(shake.peak_g, 3),
            "shakingDuration_s": round(shake.duration_s, 2),
            "estimatedMMI": shake.mmi_int,
            "node": node,
        },
    }


# ------------------------------------------------------------ the two gates


@dataclass
class Gate:
    """Decides whether a reading becomes an alert.

    Short spikes and repeat readings are the two ways a single-sensor quake
    detector produces noise: a box knocked off a table spikes hard for a few
    tens of milliseconds, and one earthquake produces a stream of readings.
    """

    min_peak_g: float = MIN_PEAK_G
    min_duration_s: float = MIN_DURATION_S
    cooldown_s: float = COOLDOWN_S
    last_at: float = 0.0
    last_rank: int = 0

    def rejection(self, shake: Shake, now: float | None = None) -> str | None:
        """Return why this reading should not alert, or None to send it."""
        if shake.peak_g > MAX_PEAK_G:
            return f"peak {shake.peak_g:.1f} g is outside sensor range; check the node"
        if shake.duration_s > MAX_DURATION_S:
            return f"duration {shake.duration_s:.0f} s is not a single event"
        if shake.peak_g < self.min_peak_g:
            return f"peak {shake.peak_g:.2f} g is below the {self.min_peak_g:.2f} g sensor floor"
        if shake.severity is None:
            return f"peak {shake.peak_g:.2f} g is MMI {shake.mmi_int}, below alert level (MMI {ALERT_MMI})"
        if shake.duration_s < self.min_duration_s:
            return (
                f"{shake.duration_s:.2f} s of shaking is under the "
                f"{self.min_duration_s:.1f} s gate; treated as a knock, not a quake"
            )

        now = time.monotonic() if now is None else now
        rank = SEVERITY_RANK[shake.severity]
        if self.last_at and rank <= self.last_rank:
            left = self.cooldown_s - (now - self.last_at)
            if left > 0:
                return (
                    f"same-or-lower severity within the {self.cooldown_s:.0f} s cooldown "
                    f"({left:.0f} s left); a stronger reading would override"
                )
        return None

    def record(self, shake: Shake, now: float | None = None) -> None:
        self.last_at = time.monotonic() if now is None else now
        self.last_rank = SEVERITY_RANK[shake.severity or "minor"]


# ------------------------------------------------------------ delivery


def post_alert(url: str, alert: dict, *, wait_seconds: float, timeout: float) -> dict:
    """POST one alert and return the job the backend hands back."""
    body = json.dumps({"alert": alert, "wait_seconds": wait_seconds}).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8") or "{}")


class Spool:
    """Alerts that could not be delivered, kept so a dropped link loses nothing.

    The backend already queues work when it is offline; this is the same idea
    one hop earlier, on the sensor link. If the API is down, the reading is
    written to disk and replayed on the next successful contact.
    """

    def __init__(self, path: Path) -> None:
        self.path = path

    def add(self, alert: dict) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(alert, sort_keys=True) + "\n")

    def pending(self) -> list[dict]:
        if not self.path.exists():
            return []
        alerts: list[dict] = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                alerts.append(json.loads(line))
            except json.JSONDecodeError:
                log(f"skipping unreadable spool line: {line[:60]}")
        return alerts

    def replace(self, alerts: list[dict]) -> None:
        if not alerts:
            self.path.unlink(missing_ok=True)
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            "".join(json.dumps(alert, sort_keys=True) + "\n" for alert in alerts),
            encoding="utf-8",
        )

    def drain(self, send) -> int:
        """Replay queued alerts; ``send`` returns True when one is delivered."""
        queued = self.pending()
        if not queued:
            return 0
        remaining = [alert for alert in queued if not send(alert)]
        self.replace(remaining)
        return len(queued) - len(remaining)


def probe_health(url: str, timeout: float = 2.0) -> str | None:
    """Ask the backend how it sees its own link. Never fatal."""
    base = url.rsplit("/alerts", 1)[0]
    try:
        with urllib.request.urlopen(f"{base}/health", timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8") or "{}")
    except (urllib.error.URLError, OSError, json.JSONDecodeError):
        return None
    return str(payload.get("connectivity", {}).get("state", "reachable"))


# ------------------------------------------------------------ input sources


class SerialPort:
    """A USB serial node read as lines, at 115200 8N1, no pyserial needed."""

    def __init__(self, path: str, baud: int = 115200) -> None:
        try:
            speed = getattr(termios, f"B{baud}")
        except AttributeError as exc:
            raise SystemExit(f"unsupported baud rate {baud}") from exc
        self.fd = os.open(path, os.O_RDONLY | os.O_NOCTTY)
        attrs = termios.tcgetattr(self.fd)
        attrs[0] &= ~(
            termios.IGNBRK
            | termios.BRKINT
            | termios.PARMRK
            | termios.ISTRIP
            | termios.INLCR
            | termios.IGNCR
            | termios.ICRNL
            | termios.IXON
        )
        attrs[1] &= ~termios.OPOST
        attrs[2] &= ~(termios.CSIZE | termios.PARENB)
        attrs[2] |= termios.CS8 | termios.CREAD | termios.CLOCAL
        attrs[3] &= ~(
            termios.ECHO | termios.ECHONL | termios.ICANON | termios.ISIG | termios.IEXTEN
        )
        attrs[4] = attrs[5] = speed
        termios.tcsetattr(self.fd, termios.TCSANOW, attrs)
        self._buffer = b""

    def __iter__(self):
        while True:
            ready, _, _ = select.select([self.fd], [], [], 0.5)
            if not ready:
                continue
            chunk = os.read(self.fd, 4096)
            if not chunk:
                return
            self._buffer += chunk
            while b"\n" in self._buffer:
                raw, self._buffer = self._buffer.split(b"\n", 1)
                yield raw.decode("utf-8", "replace").rstrip("\r")

    def close(self) -> None:
        os.close(self.fd)

    def __enter__(self) -> "SerialPort":
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


def read_lines(port: str, baud: int):
    if port in {"-", "stdin"}:
        yield from sys.stdin
        return
    path = Path(port)
    if not path.exists():
        raise SystemExit(f"no such serial device: {port}")
    if not stat.S_ISCHR(path.stat().st_mode):
        # a captured log replayed as a file, not a live tty
        yield from path.read_text(encoding="utf-8", errors="replace").splitlines()
        return
    with SerialPort(port, baud) as source:
        yield from source


# ------------------------------------------------------------ reporting


def log(message: str) -> None:
    print(message, file=sys.stderr, flush=True)


def report(job: dict, shake: Shake, elapsed: float) -> None:
    """Print what the pipeline decided, in one screen, from the job body."""
    status = job.get("status", "unknown")
    log(f"  job {job.get('id', '?')} {status} in {elapsed * 1000:.0f} ms (engine {job.get('engine')})")
    result = job.get("result") or {}
    if status != "done" or not result:
        log(f"  still running; poll GET /v1/jobs/{job.get('id', '<id>')}")
        return

    classification = result.get("classification", {})
    log(
        "  classified "
        f"hazard={classification.get('hazard')} "
        f"severity={classification.get('severity')} "
        f"product={classification.get('product')} "
        f"confidence={classification.get('confidence')}"
    )
    safety = result.get("safety", {})
    log(f"  first action: {safety.get('first_action', {}).get('en', '')}")
    communication = result.get("communication", {})
    for language in ("en", "es"):
        sms = communication.get("sms", {}).get(language, {})
        if sms.get("text"):
            log(f"  sms {language} ({sms.get('segments')} seg, {sms.get('chars')} chars): {sms['text']}")


# ------------------------------------------------------------ CLI


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Bridge a ResQ accelerometer node to POST /v1/alerts.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--port", default="-", help="serial device, or '-' for stdin")
    parser.add_argument("--baud", type=int, default=115200)
    parser.add_argument("--url", default=DEFAULT_URL, help="alert endpoint")
    parser.add_argument("--wait", type=float, default=25.0, help="seconds to wait for the pipeline")
    parser.add_argument("--timeout", type=float, default=90.0, help="HTTP timeout")
    parser.add_argument("--node", default="", help="node name reported in the identifier")
    parser.add_argument("--location", default="ResQ node - Hackathon venue", help="CAP areaDesc")
    parser.add_argument("--sender", default="ResQ on-device sensor", help="CAP senderName")
    parser.add_argument("--event", default="Earthquake Warning", help="CAP event name")
    parser.add_argument("--min-peak", type=float, default=MIN_PEAK_G, help="g, sensor floor")
    parser.add_argument("--min-duration", type=float, default=MIN_DURATION_S, help="s, spike gate")
    parser.add_argument("--cooldown", type=float, default=COOLDOWN_S, help="s between repeat alerts")
    parser.add_argument("--spool", default="data/bridge_spool.jsonl", help="file for undelivered alerts")
    parser.add_argument("--no-spool", action="store_true", help="drop alerts instead of queueing them")
    parser.add_argument("--dry-run", action="store_true", help="print the alert, do not post it")
    parser.add_argument(
        "--once", action="store_true", help="exit after the first alert instead of staying attached"
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    gate = Gate(
        min_peak_g=args.min_peak,
        min_duration_s=args.min_duration,
        cooldown_s=args.cooldown,
    )
    spool = None if args.no_spool else Spool(Path(args.spool))

    def send(alert: dict) -> bool:
        try:
            post_alert(args.url, alert, wait_seconds=args.wait, timeout=args.timeout)
        except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
            log(f"  delivery failed: {exc}")
            return False
        return True

    log(f"sensor bridge -> {args.url} (source {args.port})")
    if not args.dry_run:
        state = probe_health(args.url)
        if state is None:
            log("  backend not answering yet; readings will be spooled to disk")
        else:
            log(f"  backend link state: {state}")
        if spool is not None:
            replayed = spool.drain(send)
            if replayed:
                log(f"  replayed {replayed} spooled alert(s)")

    seen = 0
    forwarded = 0
    shown = 0
    try:
        for line in read_lines(args.port, args.baud):
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            seen += 1
            try:
                shake = parse_line(line, node=args.node)
            except ValueError as exc:
                log(f"  ignored: {exc}")
                continue

            reason = gate.rejection(shake)
            if reason is not None:
                log(f"  skipped: {reason}")
                continue

            alert = build_alert(shake, location=args.location, sender=args.sender, event=args.event)
            log(
                f"[{datetime.now().strftime('%H:%M:%S')}] "
                f"peak {shake.peak_g:.2f} g / {shake.duration_s:.1f} s "
                f"= MMI {shake.mmi_int} -> {shake.severity}"
            )

            if args.dry_run:
                print(json.dumps(alert, indent=2, ensure_ascii=False))
                shown += 1
                gate.record(shake)
                if args.once:
                    break
                continue

            if spool is not None:
                spool.drain(send)

            started = time.perf_counter()
            try:
                job = post_alert(args.url, alert, wait_seconds=args.wait, timeout=args.timeout)
            except (urllib.error.URLError, OSError, json.JSONDecodeError) as exc:
                log(f"  delivery failed: {exc}")
                if spool is not None:
                    spool.add(alert)
                    log(f"  spooled to {spool.path}; it will be replayed when the link returns")
                continue

            forwarded += 1
            gate.record(shake)
            report(job, shake, time.perf_counter() - started)
            if args.once:
                break
    except KeyboardInterrupt:
        log("\nstopped")
        return 0

    if seen and forwarded == 0 and seen == shown:
        log(f"{seen} reading(s), {shown} alert(s) written to stdout (dry run, nothing posted)")
    else:
        log(f"{seen} reading(s), {forwarded} alert(s) delivered")
    return 0


if __name__ == "__main__":
    sys.exit(main())
