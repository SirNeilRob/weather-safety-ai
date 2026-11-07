"""Alert normalisation.

Real feeds (CAP 1.2, IPAWS, NWS) disagree about shape: sometimes flat, sometimes
an ``info`` array, sometimes only a headline. This module accepts all of them and
produces one flat structure so the agents never branch on feed format.

Normalisation never raises on a missing field. A malformed alert must still
produce a cautious safety message rather than a 500 during a storm.
"""

from __future__ import annotations

from dataclasses import dataclass, field

VALID_SEVERITY = {"unknown", "minor", "moderate", "severe", "extreme"}
VALID_URGENCY = {"immediate", "expected", "future", "past", "unknown"}
VALID_CERTAINTY = {"observed", "likely", "possible", "unlikely", "unknown"}
VALID_LANGUAGES = {"en", "es", "fr", "pt"}

_MAX_AREAS = 12


def _first(info: dict, *keys: str, default: str = "") -> str:
    for key in keys:
        value = info.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
        if isinstance(value, (int, float)):
            return str(value)
    return default


def _unescape(value: str) -> str:
    return (
        value.replace("&amp;", "&")
        .replace("&lt;", "<")
        .replace("&gt;", ">")
        .replace("&#39;", "'")
        .strip()
    )


def _string_list(value: object, limit: int = _MAX_AREAS) -> list[str]:
    if isinstance(value, str):
        parts = [part.strip() for part in value.replace(";", ",").split(",")]
        return [_unescape(part) for part in parts if part][:limit]
    if isinstance(value, (list, tuple)):
        out: list[str] = []
        for item in value:
            if isinstance(item, str) and item.strip():
                out.append(item.strip())
            elif isinstance(item, dict):
                label = item.get("areaDesc") or item.get("name") or item.get("value")
                if isinstance(label, str) and label.strip():
                    out.append(label.strip())
        return out[:limit]
    return []


def _select_body(alert: dict) -> dict:
    """Unwrap CAP's info[] envelope, preserving the alert-level identifiers."""
    info = alert.get("info")
    if isinstance(info, list) and info and isinstance(info[0], dict):
        info = info[0]
    if not isinstance(info, dict):
        return alert

    merged = dict(info)
    for key in ("identifier", "sender", "senderName", "sent", "status", "msgType"):
        if key in alert and key not in merged:
            merged[key] = alert[key]
    return merged


def _valid(value: str, allowed: set[str], fallback: str) -> str:
    cleaned = (value or "").strip().lower()
    return cleaned if cleaned in allowed else fallback


@dataclass(slots=True)
class NormalizedAlert:
    id: str
    event: str
    headline: str
    description: str
    instruction: str
    severity: str
    urgency: str
    certainty: str
    areas: list[str]
    onset: str
    expires: str
    sent: str
    source: str
    language: str
    categories: list[str] = field(default_factory=list)

    @property
    def searchable_text(self) -> str:
        """Everything the classifier may read; event text first."""
        parts = [self.event, self.headline, self.description, self.instruction]
        parts.extend(self.areas)
        return " \n ".join(part for part in parts if part)

    @property
    def area_text(self) -> str:
        return ", ".join(self.areas[:3]) if self.areas else "the affected area"

    @property
    def explicit_severity(self) -> bool:
        return self.severity in {"minor", "moderate", "severe", "extreme"}

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "title": self.event,
            "headline": self.headline,
            "description": self.description,
            "instruction": self.instruction,
            "severity": self.severity,
            "urgency": self.urgency,
            "certainty": self.certainty,
            "areas": self.areas,
            "area_text": self.area_text,
            "onset": self.onset,
            "expires": self.expires,
            "sent": self.sent,
            "source": self.source,
            "language": self.language,
            "categories": self.categories,
        }


def normalize(raw: dict | None) -> NormalizedAlert:
    """Tolerant CAP / flat-JSON alert normalisation."""
    source = raw if isinstance(raw, dict) else {}
    body = _select_body(source)

    event = _unescape(_first(body, "event", "title", "eventCode", "headline", default="Weather alert"))
    headline = _unescape(_first(body, "headline", "subtitle", "summary"))
    description = _unescape(_first(body, "description", "descriptionText", "details", "body", "summary"))
    instruction = _unescape(
        _first(body, "instruction", "instructions", "recommendedAction", "action", "guidance")
    )

    areas = _string_list(body.get("areaDesc") or body.get("areas") or body.get("area"))
    categories = _string_list(body.get("category") or body.get("categories"))

    return NormalizedAlert(
        id=_first(body, "identifier", "id", "alertId"),
        event=event[:300],
        headline=headline[:500],
        description=description[:6000],
        instruction=instruction[:4000],
        severity=_valid(_first(body, "severity"), VALID_SEVERITY, "unknown"),
        urgency=_valid(_first(body, "urgency"), VALID_URGENCY, "unknown"),
        certainty=_valid(_first(body, "certainty"), VALID_CERTAINTY, "unknown"),
        areas=areas,
        onset=_first(body, "onset", "effective", "starts", "start"),
        expires=_first(body, "expires", "ends", "end"),
        sent=_first(body, "sent", "sentDate", "issued"),
        language=_valid(_first(body, "language", "lang"), VALID_LANGUAGES, "en"),
        source=_unescape(_first(body, "senderName", "sender", "source", default="Unknown source")),
        categories=categories,
    )
