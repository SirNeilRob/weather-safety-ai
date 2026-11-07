from __future__ import annotations

import math

import pytest

from app.agents.cloud import CloudDrafter
from app.agents.communication import sms_meta
from app.agents.pipeline import AlertPipeline
from app.domain.alert import normalize
from tests.fixtures.samples import SAMPLES


def sample(name: str) -> dict:
    for entry in SAMPLES:
        if entry["name"] == name:
            return entry
    raise AssertionError(f"no sample named {name}")


@pytest.fixture
def pipeline() -> AlertPipeline:
    return AlertPipeline()


def expected_for(key: str, result: dict):
    """Map a fixture expectation onto the place the pipeline actually reports it."""
    if key in {"hazard", "severity", "product", "escalated"}:
        return result["classification"][key]
    if key == "assumed":
        return result["classification"]["severity_assumed"]
    if key == "evacuation":
        return result["safety"]["evacuation_recommended"]
    if key == "shelter":
        return result["safety"]["shelter_recommended"]
    if key == "language":
        return result["alert"]["language"]
    raise AssertionError(f"unmapped expectation {key}")


@pytest.mark.anyio
@pytest.mark.parametrize("entry", SAMPLES, ids=[entry["name"] for entry in SAMPLES])
async def test_every_sample_matches_its_documented_expectation(entry, pipeline) -> None:
    result = await pipeline.run(entry["alert"])
    for key, value in entry["expected"].items():
        assert expected_for(key, result) == value, f"{entry['name']}: {key}"


@pytest.mark.anyio
async def test_pipeline_runs_fully_offline_and_traces_the_handoff_chain(pipeline) -> None:
    result = await pipeline.run(sample("tornado_emergency")["alert"])
    assert result["offline"] is True
    assert result["engine"] == "local"
    assert [step["agent"] for step in result["trace"]] == ["weather", "safety", "communication"]
    assert all(step["engine"] == "local" for step in result["trace"])
    # each hop hands something forward, which is what makes the chain real
    assert "hazard=tornado" in result["trace"][0]["handoff"]
    assert "immediate" in result["trace"][1]["handoff"]


@pytest.mark.anyio
async def test_extreme_tornado_escalates_from_body_text_and_keeps_the_higher_value(pipeline) -> None:
    result = await pipeline.run(sample("tornado_emergency")["alert"])
    classification = result["classification"]
    assert classification["hazard"] == "tornado"
    assert classification["severity"] == "extreme"
    assert classification["product"] == "warning"
    assert classification["severity_rank"] == 4
    assert any("tornado" in signal for signal in classification["signals"])


@pytest.mark.anyio
async def test_understated_heat_alert_is_escalated_and_flagged(pipeline) -> None:
    result = await pipeline.run(sample("heat_understated")["alert"])
    classification = result["classification"]
    assert classification["severity"] == "extreme"
    assert classification["escalated"] is True
    assert classification["conflicts"]


@pytest.mark.anyio
async def test_actionable_advice_is_curated_not_machine_translated(pipeline) -> None:
    result = await pipeline.run(sample("tornado_emergency")["alert"])
    safety = result["safety"]
    assert safety["first_action_source"] == "playbook"
    # the Spanish line must be clean Spanish, not a half-translated mix
    assert "shelter" not in safety["first_action"]["es"].lower()
    assert "the " not in safety["first_action"]["es"].lower()
    # the feed's own wording is still available, with its quality labelled
    official = safety["official_instruction"]
    assert official["text"]["en"].startswith("Take shelter now")
    assert official["method"]["es"] == "phrasebook"
    assert 0.0 <= official["coverage"]["es"] <= 1.0


@pytest.mark.anyio
async def test_safety_plan_covers_all_three_phases_in_both_languages(pipeline) -> None:
    result = await pipeline.run(sample("flash_flood")["alert"])
    safety = result["safety"]
    assert safety["counts"] == {
        "immediate": len(safety["immediate"]),
        "avoid": len(safety["avoid"]),
        "prepare": len(safety["prepare"]),
    }
    for item in safety["immediate"] + safety["avoid"] + safety["prepare"]:
        assert item["en"] and item["es"]


@pytest.mark.anyio
async def test_sms_stays_inside_one_gsm7_segment_for_english(pipeline) -> None:
    result = await pipeline.run(sample("tornado_emergency")["alert"])
    sms = result["communication"]["sms"]["en"]
    assert sms["encoding"] == "gsm7"
    assert sms["segments"] == 1
    assert sms["chars"] == len(sms["text"]) <= 160
    assert "Tornado" in sms["text"]


@pytest.mark.anyio
async def test_spanish_sms_uses_ucs2_budget_honestly(pipeline) -> None:
    result = await pipeline.run(sample("tornado_emergency")["alert"])
    sms = result["communication"]["sms"]["es"]
    assert sms["encoding"] == "ucs2"
    assert sms["chars"] == len(sms["text"])
    expected_segments = 1 if sms["chars"] <= 70 else math.ceil(sms["chars"] / 67)
    assert sms["segments"] == expected_segments
    assert "Aviso de tornado" in sms["text"]


def test_sms_metadata_boundaries() -> None:
    assert sms_meta("A" * 160) == {"chars": 160, "encoding": "gsm7", "segments": 1}
    assert sms_meta("A" * 161)["segments"] == 2
    assert sms_meta("A" * 306)["segments"] == 2
    assert sms_meta("A" * 307)["segments"] == 3
    # "é" is in the GSM 03.38 default alphabet; "ó" is not and forces UCS-2.
    assert sms_meta("é" * 160)["encoding"] == "gsm7"
    assert sms_meta("ó" * 70) == {"chars": 70, "encoding": "ucs2", "segments": 1}
    assert sms_meta("ó" * 71)["segments"] == 2
    assert sms_meta("ó" * 134)["segments"] == 2
    assert sms_meta("ó" * 135)["segments"] == 3


@pytest.mark.anyio
async def test_missing_area_is_reported_as_a_warning(pipeline) -> None:
    result = await pipeline.run({"event": "Tornado Warning", "severity": "Severe"})
    assert any("area" in warning for warning in result["warnings"])


@pytest.mark.anyio
async def test_spanish_source_alert_is_normalised_and_detected(pipeline) -> None:
    result = await pipeline.run(sample("spanish_flood")["alert"])
    assert result["alert"]["language"] == "es"
    assert result["classification"]["hazard"] == "flood"
    # Spanish source means the English line has to be produced, and vice versa
    assert result["communication"]["sms"]["en"]["text"]


def test_cap_style_alert_is_normalised() -> None:
    alert = normalize(
        {
            "identifier": "CAP-1",
            "info": [
                {
                    "event": "Flash Flood Warning",
                    "severity": "Severe",
                    "urgency": "Immediate",
                    "certainty": "Observed",
                    "area": [{"areaDesc": "Travis County"}],
                    "headline": "Flash flooding",
                }
            ],
        }
    )
    assert alert.event == "Flash Flood Warning"
    assert alert.severity == "severe"
    assert alert.areas == ["Travis County"]


class _StubSwarm:
    """Stands in for the Swarm client without needing a network."""

    configured = True

    def __init__(self, payload) -> None:
        self.payload = payload

    async def draft_communication(self, **kwargs):
        return self.payload


class _Monitor:
    def is_online(self) -> bool:
        return True


def _cloud(payload) -> CloudDrafter:
    return CloudDrafter(_StubSwarm(payload), _Monitor())


def cloud_payload(sms_en: str = "Tornado Warning. EXTREME. Shelter now.") -> dict:
    return {
        "title": {"en": "Tornado Warning", "es": "Aviso de tornado"},
        "sms": {"en": {"text": sms_en}, "es": {"text": "Aviso de tornado. Refúgiese."}},
        "push": {
            "en": {"title": "Tornado Warning", "body": "Shelter now."},
            "es": {"title": "Aviso de tornado", "body": "Refúgiese ahora."},
        },
        "bulletin": {"en": "Bulletin", "es": "Boletín"},
        "voice": {"en": "Voice", "es": "Voz"},
    }


@pytest.mark.anyio
async def test_cloud_stage_keeps_the_deterministic_envelope() -> None:
    pipeline = AlertPipeline(cloud=_cloud(cloud_payload()))
    result = await pipeline.run(sample("tornado_emergency")["alert"])

    # Only the communication hop is delegated, so the pipeline is "mixed" —
    # claiming "swarm" would overstate what the model did.
    assert result["engine"] == "mixed"
    assert [step["engine"] for step in result["trace"]] == ["local", "local", "swarm"]

    communication = result["communication"]
    # Prose came from Swarm; the badge and the billing numbers did not.
    assert communication["title"]["en"] == "Tornado Warning"
    assert communication["severity"] == {
        "level": "extreme",
        "label": {"en": "Extreme", "es": "Extrema"},
        "color": "#6b0f0f",
    }
    assert communication["sms"]["en"]["segments"] == 1
    assert communication["sms"]["en"]["encoding"] == "gsm7"
    assert communication["translation"] == {}
    assert any("Swarm" in warning for warning in result["warnings"])


@pytest.mark.anyio
async def test_overlong_cloud_sms_falls_back_to_the_local_composer() -> None:
    pipeline = AlertPipeline(cloud=_cloud(cloud_payload(sms_en="x" * 200)))
    result = await pipeline.run(sample("tornado_emergency")["alert"])

    assert result["engine"] == "local"
    assert all(step["engine"] == "local" for step in result["trace"])
    # The local SMS is fitted to the carrier budget, not the model's text.
    assert result["communication"]["sms"]["en"]["chars"] <= 160
