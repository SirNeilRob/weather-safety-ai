from __future__ import annotations

import pytest

from app.knowledge.hazards import (
    ACTIONS,
    HAZARDS,
    PRODUCT_TYPES,
    RANK,
    SEVERITY_LABELS,
    detected_product_type,
    playbook,
    severity_at_least,
)


def test_severity_ranks_are_ordered_and_labels_cover_every_level() -> None:
    ranks = [RANK[name] for name in ("unknown", "minor", "moderate", "severe", "extreme")]
    assert ranks == sorted(ranks)
    assert set(SEVERITY_LABELS) == set(RANK)


def test_every_playbook_action_is_authored_in_both_languages() -> None:
    for action in ACTIONS:
        assert action.en.strip()
        assert action.es.strip()
        # curated text, not a machine render: the Spanish must differ from English
        assert action.es != action.en
        assert action.hazard in HAZARDS
        assert action.phase in {"immediate", "avoid", "prepare"}
        assert action.min_severity in RANK


def test_extreme_tornado_gets_shelter_advice_not_evacuation() -> None:
    plan = playbook("tornado", "extreme")
    assert plan["shelter_recommended"] is True
    assert plan["evacuation_recommended"] is False
    assert plan["immediate"], "an extreme tornado must always produce immediate actions"
    combined = " ".join(item["en"].lower() for item in plan["immediate"])
    assert "basement" in combined or "interior room" in combined
    # bilingual parity: every English action has a Spanish counterpart
    for item in plan["immediate"] + plan["avoid"] + plan["prepare"]:
        assert item["es"].strip() and item["es"] != item["en"]


def test_severe_flood_sends_people_to_high_ground_not_out_of_the_area() -> None:
    severe = playbook("flood", "severe")
    assert severe["evacuation_recommended"] is False
    assert severe["shelter_recommended"] is False
    assert "terreno elevado" in severe["immediate"][0]["es"].lower()
    # an extreme flood is a different story: leaving the area is the advice
    assert playbook("flood", "extreme")["evacuation_recommended"] is True


def test_severe_wildfire_recommends_evacuation() -> None:
    assert playbook("wildfire", "severe")["evacuation_recommended"] is True


def test_minor_events_are_gated_to_fewer_actions_than_extreme() -> None:
    minor = playbook("thunderstorm", "minor")
    extreme = playbook("thunderstorm", "extreme")
    assert len(minor["immediate"]) <= len(extreme["immediate"])


def test_missing_severity_falls_back_to_moderate_tier_with_instructions() -> None:
    assert severity_at_least("tornado", "unknown") == "moderate"
    plan = playbook("tornado", "unknown")
    assert plan["tier"] == "moderate"
    assert plan["immediate"], "an unknown severity must not leave the user with no advice"


def test_unknown_hazard_still_gets_the_generic_playbook() -> None:
    plan = playbook("volcano", "severe")
    assert plan["immediate"]
    assert "follow instructions" in plan["immediate"][0]["en"].lower()


@pytest.mark.parametrize(
    "event,expected",
    [
        ("Tornado Warning", "warning"),
        ("Flash Flood Emergency", "emergency"),
        ("Flash Flood Watch", "watch"),
        ("Dense Fog Advisory", "advisory"),
        ("Air Quality Alert", "alert"),
        ("Earthquake", None),
        ("", None),
    ],
)
def test_product_detection_prefers_the_strongest_product_word(event, expected) -> None:
    assert detected_product_type(event) == expected


def test_product_floors_only_raise_severity() -> None:
    for meta in PRODUCT_TYPES.values():
        assert meta["floor"] in RANK
        assert RANK[meta["floor"]] >= RANK["minor"]
        assert meta["en"] and meta["es"]
