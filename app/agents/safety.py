"""Safety agent — turns a classification into actions a person can follow.

Input is the weather agent's output, never the raw alert alone. That is the
handoff: severity and hazard drive which actions are selected, so an Extreme
tornado gets "protect your head" while a Minor wind advisory does not.

The action text comes from the curated bilingual playbook, so both languages are
authored, not translated. Only the feed's own free-text instruction is passed
through the phrasebook, and that path is labelled.
"""

from __future__ import annotations

from typing import Any

from ..domain.alert import NormalizedAlert
from ..i18n.terms import translate
from ..knowledge.hazards import SEVERITY_LABELS, playbook


def _official_instruction(alert: NormalizedAlert) -> dict[str, Any] | None:
    if not alert.instruction.strip():
        return None

    source_language = alert.language
    out: dict[str, Any] = {
        "source_language": source_language,
        "text": {"en": "", "es": ""},
        "method": {"en": "verbatim", "es": "verbatim"},
        "coverage": {"en": 1.0, "es": 1.0},
    }

    for language in ("en", "es"):
        if source_language == language:
            out["text"][language] = alert.instruction
            continue
        translated = translate(alert.instruction, language)
        out["text"][language] = translated.text
        out["method"][language] = translated.method
        out["coverage"][language] = round(translated.coverage, 3)

    return out


def build(alert: NormalizedAlert, classification: dict[str, Any]) -> dict[str, Any]:
    hazard = classification.get("hazard", "other")
    severity = classification.get("severity", "unknown")

    plan = playbook(hazard, severity)
    severity_meta = SEVERITY_LABELS.get(classification.get("severity", "unknown"), SEVERITY_LABELS["unknown"])

    immediate = plan["immediate"]
    avoid = plan["avoid"]
    prepare = plan["prepare"]

    first_action = {
        "en": immediate[0]["en"] if immediate else "Follow instructions from local officials.",
        "es": immediate[0]["es"] if immediate else "Siga las instrucciones de las autoridades locales.",
    }

    official = _official_instruction(alert)

    # ``first_action`` stays on the curated playbook on purpose. The feed's own
    # instruction is shown next to it, in its own field, with the method and
    # coverage of its translation attached: the line a person acts on is never
    # a partial phrasebook render such as "Beba agua often y verifique en older
    # neighbors", while the official wording is still available verbatim.

    return {
        "tier": plan["tier"],
        "tier_label": {"en": severity_meta["en"], "es": severity_meta["es"]},
        "advice": {"en": severity_meta["advice"], "es": severity_meta["consejo"]},
        "color": severity_meta["color"],
        "immediate": immediate,
        "avoid": avoid,
        "prepare": prepare,
        "evacuation_recommended": plan["evacuation_recommended"],
        "shelter_recommended": plan["shelter_recommended"],
        "first_action": first_action,
        "first_action_source": "playbook",
        "official_instruction": official,
        "official_reliable": {
            "en": bool(official and official["coverage"]["en"] >= 0.9),
            "es": bool(official and official["coverage"]["es"] >= 0.9),
        },
        "counts": {
            "immediate": len(immediate),
            "avoid": len(avoid),
            "prepare": len(prepare),
        },
    }


class SafetyAgent:
    name = "safety"
    stage = "plan"

    def run(self, alert: NormalizedAlert, classification: dict[str, Any]) -> dict[str, Any]:
        return build(alert, classification)
