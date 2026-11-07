"""Weather agent — hazard and severity classification.

Pure function over the normalised alert. Same input, same output, no network,
no model download. That determinism is what makes the offline mode testable:
the Integration engineer can assert on exact values instead of "it looked right".

Escalation policy: text evidence may only raise severity. A feed that says
"Minor" while its own description says "catastrophic" is classified Extreme and
the contradiction is reported. Never silently downgrade a warning.
"""

from __future__ import annotations

from typing import Any

from ..domain.alert import NormalizedAlert
from ..i18n.terms import translate
from ..knowledge.hazards import (
    CAP_EVENT_HAZARD,
    HAZARD_KEYWORDS,
    HAZARDS,
    PRODUCT_TYPES,
    RANK,
    SEVERITY_SIGNALS,
    detected_product_type,
    numeric_evidence,
)

MIN_HAZARD_SCORE = 3.0


def _bilingual_text(alert: NormalizedAlert) -> tuple[str, str]:
    """Return (event text, body text) with an English rendering alongside.

    The knowledge base is written in English, so a Spanish alert is matched on
    both its own words and their English rendering. Without this, "Aviso de
    inundación" classified as "other" and the user got the generic playbook
    instead of flood advice.
    """
    event_text = alert.event.lower()
    body = f"{alert.headline} {alert.description} {alert.instruction}".lower()
    if alert.language in {"en", ""}:
        return event_text, body
    english = translate(f"{alert.event} {body}", "en").text.lower()
    return f"{event_text} {english}", f"{body} {english}"


def _hazard_scores(alert: NormalizedAlert) -> dict[str, float]:
    event_text, body = _bilingual_text(alert)

    scores: dict[str, float] = {}
    for hazard, keywords in HAZARD_KEYWORDS.items():
        score = 0.0
        for phrase, weight in keywords.items():
            if phrase in event_text:
                score += weight * 2.0  # the event name is the strongest signal
            elif phrase in body:
                score += weight
        scores[hazard] = score

    # exact CAP event lookup beats fuzzy matching
    key = alert.event.strip().lower()
    mapped = CAP_EVENT_HAZARD.get(key)
    if mapped:
        scores[mapped] = scores.get(mapped, 0.0) + 8.0
    else:
        for event_name, hazard in CAP_EVENT_HAZARD.items():
            if event_name in key:
                scores[hazard] = scores.get(hazard, 0.0) + 5.0
                break

    return scores


def _urgency(alert: NormalizedAlert, product: str | None) -> str:
    if alert.urgency != "unknown":
        return alert.urgency
    if product:
        return PRODUCT_TYPES[product]["urgency"]
    return "unknown"


def _certainty(alert: NormalizedAlert) -> str:
    if alert.certainty != "unknown":
        return alert.certainty
    text = alert.searchable_text.lower()
    if any(term in text for term in ("confirmed", "on the ground", "observed", "reported")):
        return "observed"
    if "radar indicated" in text:
        return "likely"
    if any(term in text for term in ("possible", "could", "may")):
        return "possible"
    return "unknown"


def classify(alert: NormalizedAlert) -> dict[str, Any]:
    scores = _hazard_scores(alert)
    ranked = sorted(scores.items(), key=lambda item: item[1], reverse=True)
    best_hazard, best_score = ranked[0] if ranked else ("other", 0.0)
    hazard = best_hazard if best_score >= MIN_HAZARD_SCORE else "other"

    signals: list[str] = []
    conflicts: list[str] = []

    # ---------------------------------------------------------------- severity
    severity = alert.severity
    explicit_rank = RANK.get(severity, 0)

    product = detected_product_type(alert.event)
    if product:
        floor = PRODUCT_TYPES[product]["floor"]
        if RANK[floor] > RANK.get(severity, 0):
            signals.append(f"product type '{product}' sets a severity floor of {floor}")
            severity = floor

    text = alert.searchable_text
    if alert.language not in {"en", ""}:
        # Escalation phrasing ("do not wait", "catastrophic") must still be seen
        # when the feed is Spanish, so the English rendering is searched too.
        text = f"{text} {translate(text, 'en').text}"
    for signal in SEVERITY_SIGNALS:
        if signal.pattern.search(text):
            if RANK[signal.severity] > RANK.get(severity, 0):
                signals.append(f"'{signal.reason}' raised severity to {signal.severity}")
                severity = signal.severity

    for level, reason in numeric_evidence(text, hazard):
        if RANK[level] > RANK.get(severity, 0):
            signals.append(f"{reason} raised severity to {level}")
            severity = level

    escalated = bool(alert.explicit_severity and RANK.get(severity, 0) > explicit_rank)

    if escalated:
        conflicts.append(
            f"feed declared '{alert.severity}' but the alert body indicates '{severity}'; "
            "kept the higher value"
        )

    assumed = False
    if RANK.get(severity, 0) == 0:
        assumed = True
        signals.append("no severity stated by the feed; safety tier defaults to moderate")

    # ---------------------------------------------------------------- hazard detail
    if hazard != "other":
        top_evidence = [
            phrase
            for phrase in HAZARD_KEYWORDS.get(hazard, {})
            if phrase in text.lower()
        ]
        if product:
            signals.append(f"event is a {product}")
        if best_hazard in HAZARDS:
            signals.append(
                f"hazard keywords matched {len(top_evidence)} term(s) for {hazard}"
            )
    else:
        signals.append("no hazard family matched strongly; using the generic playbook")

    # ---------------------------------------------------------------- confidence
    confidence = 0.30
    if alert.explicit_severity:
        confidence += 0.25
    if hazard != "other" and best_score >= 5.0:
        confidence += 0.20
    elif hazard != "other":
        confidence += 0.10
    if any(reason for _, reason in numeric_evidence(text, hazard)):
        confidence += 0.10
    if product:
        confidence += 0.05
    if product and product in alert.event.lower():
        confidence += 0.05
    confidence = min(0.97, confidence)
    if hazard == "other":
        confidence = min(confidence, 0.35)

    uncertainty = alert.certainty == "unknown" and not any("radar" in s for s in signals)
    if uncertainty and confidence > 0.9:
        confidence = 0.9

    rationale_parts = [f"Classified as {hazard} ({HAZARDS[hazard].en})"]
    rationale_parts.append(f"severity {severity}")
    if signals:
        rationale_parts.append("based on " + "; ".join(signals[:3]))
    rationale = ". ".join(rationale_parts) + "."

    return {
        "hazard": hazard,
        "hazard_label": {"en": HAZARDS[hazard].en, "es": HAZARDS[hazard].es},
        "hazard_icon": HAZARDS[hazard].icon,
        "severity": severity,
        "severity_rank": RANK.get(severity, 0),
        "severity_assumed": assumed,
        "escalated": escalated,
        "product": product,
        "urgency": _urgency(alert, product),
        "certainty": _certainty(alert),
        "confidence": round(confidence, 2),
        "signals": signals[:8],
        "conflicts": conflicts,
        "rationale": rationale,
        "scores": {name: round(value, 1) for name, value in ranked[:5] if value > 0},
    }


class WeatherAgent:
    name = "weather"
    stage = "classify"

    def run(self, alert: NormalizedAlert) -> dict[str, Any]:
        return classify(alert)
