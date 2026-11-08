"""Tests for the microcontroller -> /v1/alerts bridge.

The bridge deliberately does not import the app: it is the field-side script
that runs next to a microcontroller, possibly on a different machine. So these
tests exercise it the way it runs, through its public functions and its CLI.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("sensor_bridge", ROOT / "scripts" / "sensor_bridge.py")
assert SPEC and SPEC.loader
sensor_bridge = importlib.util.module_from_spec(SPEC)
sys.modules["sensor_bridge"] = sensor_bridge
SPEC.loader.exec_module(sensor_bridge)

parse_line = sensor_bridge.parse_line
build_alert = sensor_bridge.build_alert
Gate = sensor_bridge.Gate
Spool = sensor_bridge.Spool
Shake = sensor_bridge.Shake


# ------------------------------------------------------------------ parsing


@pytest.mark.parametrize(
    "line",
    [
        "PEAK=0.21 DUR=2.4",
        "peak=0.21g dur=2.4s",
        "PEAK=210mg DUR=2400ms",
        "0.21,2.4",
        '{"peak_g": 0.21, "duration_s": 2.4}',
        '{"peak_mg": 210, "dur_ms": 2400}',
    ],
)
def test_every_wire_format_means_the_same_event(line: str) -> None:
    shake = parse_line(line)
    assert shake.peak_g == pytest.approx(0.21)
    assert shake.duration_s == pytest.approx(2.4)


def test_a_missing_suffix_uses_the_unit_the_key_implies() -> None:
    # peak_mg without a suffix is still milligee, not g: a firmware rewrite must
    # not turn a 0.21 g reading into a 210 g one.
    assert parse_line('{"peak_mg": 210, "duration_s": 2.4}').peak_g == pytest.approx(0.21)
    assert parse_line('{"peak_g": 0.21, "dur_ms": 2400}').duration_s == pytest.approx(2.4)


@pytest.mark.parametrize(
    "line",
    ["", "# boot", "garbage", "PEAK=0.21", "PEAK= DUR=2.4", "PEAK=-1 DUR=2"],
)
def test_bad_lines_are_rejected_without_crashing(line: str) -> None:
    with pytest.raises(ValueError):
        parse_line(line)


def test_json_line_can_carry_the_node_name() -> None:
    shake = parse_line('{"peak_g": 0.21, "duration_s": 2.4, "node": "resq-01"}')
    assert shake.node == "resq-01"


# ------------------------------------------------------- intensity mapping


@pytest.mark.parametrize(
    ("peak_g", "mmi", "severity"),
    [
        (0.01, 2, None),  # background noise, below the formula's useful range
        (0.04, 4, None),  # felt, but not an alert
        (0.05, 5, "minor"),
        (0.10, 6, "moderate"),
        (0.21, 7, "severe"),
        (0.50, 8, "extreme"),
    ],
)
def test_shaking_intensity_drives_the_severity(peak_g: float, mmi: int, severity) -> None:
    shake = Shake(peak_g=peak_g, duration_s=3.0)
    assert shake.mmi_int == mmi
    assert shake.severity == severity


def test_the_reference_reading_is_seven_and_severe() -> None:
    shake = parse_line("PEAK=0.21 DUR=2.4")
    assert shake.mmi_int == 7
    assert shake.severity == "severe"
    assert shake.description == "Peak 0.21 g sustained for 2.4 s"


# ------------------------------------------------------------------- gates


def test_a_knock_is_rejected_but_a_quake_is_not() -> None:
    gate = Gate()
    # a board knocked off the table: huge peak, no duration
    dropped = Shake(peak_g=2.4, duration_s=0.04)
    assert "treated as a knock" in str(gate.rejection(dropped))
    # a real MMI VII quake
    assert gate.rejection(Shake(peak_g=0.21, duration_s=2.4)) is None


def test_a_reading_below_the_sensor_floor_is_explained() -> None:
    reason = Gate().rejection(Shake(peak_g=0.01, duration_s=5.0))
    assert reason is not None
    assert "sensor floor" in reason


def test_one_earthquake_produces_one_alert_until_it_escalates() -> None:
    gate = Gate(cooldown_s=120.0)
    first = Shake(peak_g=0.21, duration_s=2.4)
    assert gate.rejection(first, now=1000.0) is None
    gate.record(first, now=1000.0)

    # the same shaking, ten seconds later, is the same earthquake
    repeat = Shake(peak_g=0.22, duration_s=2.6)
    reason = gate.rejection(repeat, now=1010.0)
    assert reason is not None and "cooldown" in reason

    # ... but worse shaking overrides the cooldown
    worse = Shake(peak_g=0.60, duration_s=3.0)
    assert worse.severity == "extreme"
    assert gate.rejection(worse, now=1010.0) is None


def test_the_cooldown_expires() -> None:
    gate = Gate(cooldown_s=120.0)
    shake = Shake(peak_g=0.21, duration_s=2.4)
    gate.record(shake, now=1000.0)
    assert gate.rejection(shake, now=1200.0) is None


# ------------------------------------------------------------- alert shape


def test_alert_has_the_shape_the_pipeline_already_ingests() -> None:
    shake = parse_line("PEAK=0.21 DUR=2.4")
    alert = build_alert(
        shake,
        location="ResQ node - Hackathon venue",
        sender="ResQ on-device sensor",
        event="Earthquake Warning",
    )
    assert alert["event"] == "Earthquake Warning"
    assert alert["severity"] == "Severe"
    assert alert["urgency"] == "Immediate"
    assert alert["certainty"] == "Observed"
    assert alert["areaDesc"] == "ResQ node - Hackathon venue"
    assert alert["headline"] == "Strong shaking detected by ResQ sensor"
    assert alert["description"] == "Peak 0.21 g sustained for 2.4 s"
    assert alert["senderName"] == "ResQ on-device sensor"
    # the numbers travel with the alert, so the dashboard can show its working
    assert alert["parameters"]["peakAcceleration_g"] == pytest.approx(0.21)
    assert alert["parameters"]["estimatedMMI"] == 7


def test_alert_identifier_is_unique_per_event() -> None:
    shake = parse_line("PEAK=0.21 DUR=2.4")
    first = build_alert(shake, location="x", sender="s", event="Earthquake Warning")
    second = build_alert(shake, location="x", sender="s", event="Earthquake Warning")
    assert first["identifier"] != second["identifier"]


# ------------------------------------------------------------------- spool


def test_undelivered_alerts_are_kept_and_replayed(tmp_path: Path) -> None:
    spool = Spool(tmp_path / "spool" / "queue.jsonl")
    alert = {"event": "Earthquake Warning", "severity": "Severe"}
    spool.add(alert)
    spool.add(alert)
    assert len(spool.pending()) == 2

    # the link is back: both replay, and the file is cleaned up
    assert spool.drain(lambda item: True) == 2
    assert spool.pending() == []


def test_a_still_broken_link_keeps_the_queue(tmp_path: Path) -> None:
    spool = Spool(tmp_path / "queue.jsonl")
    spool.add({"event": "Earthquake Warning"})
    assert spool.drain(lambda item: False) == 0
    assert len(spool.pending()) == 1


# --------------------------------------------------------------------- CLI


def test_cli_dry_run_prints_the_alert_without_posting(capsys) -> None:
    argc = sensor_bridge.main(
        [
            "--once",
            "--dry-run",
            "--no-spool",
            "--port",
            str(ROOT / "tests" / "data" / "shake.log"),
        ]
    )
    assert argc == 0
    captured = capsys.readouterr()
    payload = json.loads(captured.out)
    assert payload["event"] == "Earthquake Warning"
    assert payload["severity"] == "Severe"
    # the noise line and the knock in the same capture never became alerts
    assert captured.err.count("skipped") == 2
