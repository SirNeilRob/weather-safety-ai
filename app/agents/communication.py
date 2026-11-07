"""Communication agent — turns a safety plan into deliverable messages.

Produces four channels, in both languages:

* ``sms``      — one short message, with real GSM-7/UCS-2 segment accounting
* ``push``     — notification title and body
* ``bulletin`` — the full dashboard / radio copy
* ``voice``    — a short script for text-to-speech or a phone tree

The SMS length work is not cosmetic. Spanish text with accents drops out of
GSM-7 into UCS-2, which cuts the per-segment budget from 160 to 70 characters,
so a message that looks like 120 characters can bill as two segments. The agent
reports ``encoding`` and ``segments`` instead of silently sending a longer bill.
"""

from __future__ import annotations

from typing import Any

from ..domain.alert import NormalizedAlert
from ..i18n.terms import translate
from ..knowledge.hazards import HAZARDS, PRODUCT_TYPES, SEVERITY_LABELS
from .base import fit, sentences

SMS_LIMIT = 160

#: Phrasebook coverage below which free-text is left in its source language.
#: Word-by-word substitution over prose it does not know produces Spanglish
#: ("the río se ha desbordado and the water sube rápidamente"), which is worse
#: in a life-safety bulletin than an honest untranslated sentence.
MIN_FREE_TEXT_COVERAGE = 0.85

#: GSM 03.38 basic character set.
GSM7_CHARS = frozenset(
    "@£$¥èéùìòÇ\nØø\rÅåΔ_ΦΓΛΩΠΨΣΘΞÆæßÉ !\"#¤%&'()*+,-./0123456789:;<=>?"
    "¡ABCDEFGHIJKLMNOPQRSTUVWXYZÄÖÑÜ§¿abcdefghijklmnopqrstuvwxyzäöñüà"
)


def _label_for(hazard: str, language: str) -> str:
    meta = HAZARDS.get(hazard, HAZARDS["other"])
    return meta.en if language == "en" else meta.es


def _title(classification: dict, language: str) -> str:
    """Build the public title from curated labels, never by translating.

    Machine word-order would produce "Tornado aviso" in Spanish; composing from
    the curated product and hazard labels gives "Aviso de tornado".
    """
    hazard = classification.get("hazard", "other")
    product = classification.get("product")
    label = _label_for(hazard, language)
    if not product:
        return label
    if language == "en":
        return f"{label} {PRODUCT_TYPES[product]['en']}"
    return f"{PRODUCT_TYPES[product]['es']} de {label.lower()}"


def _severity_word(severity: str, language: str) -> str:
    meta = SEVERITY_LABELS.get(severity, SEVERITY_LABELS["unknown"])
    return str(meta["en"] if language == "en" else meta["es"])


def severity_block(severity: str) -> dict[str, Any]:
    """The severity badge every channel response carries."""
    meta = SEVERITY_LABELS.get(severity, SEVERITY_LABELS["unknown"])
    return {
        "level": severity,
        "label": {"en": meta["en"], "es": meta["es"]},
        "color": meta["color"],
    }


def sms_meta(text: str) -> dict[str, Any]:
    """Segment accounting using the real GSM-7 / UCS-2 budgets."""
    gsm7 = all(char in GSM7_CHARS for char in text)
    length = len(text)
    if gsm7:
        segments = 1 if length <= 160 else -(-length // 153)
    else:
        segments = 1 if length <= 70 else -(-length // 67)
    return {
        "chars": length,
        "encoding": "gsm7" if gsm7 else "ucs2",
        "segments": segments,
    }


def _what_happened(alert: NormalizedAlert, language: str) -> tuple[str, dict[str, Any]]:
    source_text = alert.description or alert.headline or alert.event
    summary = " ".join(sentences(source_text, 2)) if source_text else ""
    if not summary:
        return "", {"method": "none", "coverage": 1.0}

    if alert.language == language:
        return summary, {"method": "verbatim", "coverage": 1.0}

    translated = translate(summary, language)
    meta = {
        "method": translated.method,
        "coverage": round(translated.coverage, 3),
        "unknown_terms": translated.unknown_terms[:10],
    }
    if translated.coverage < MIN_FREE_TEXT_COVERAGE:
        note = " (original text, not translated)" if language == "en" else " (texto original, sin traducir)"
        return f"{summary}{note}", {**meta, "method": "untranslated"}
    return translated.text, meta


def build(alert: NormalizedAlert, classification: dict, safety: dict) -> dict[str, Any]:
    severity = classification.get("severity", "unknown")
    area = alert.area_text
    source = alert.source or "Local authority"
    expires = alert.expires

    messages: dict[str, Any] = {"sms": {}, "push": {}, "bulletin": {}, "voice": {}}
    translation_report: dict[str, Any] = {}

    for language in ("en", "es"):
        title = _title(classification, language)
        severity_word = _severity_word(severity, language)
        action = safety["first_action"][language]

        # ------------------------------------------------------------ SMS
        if severity == "unknown":
            sms_body = f"{title}. {area}. {action} - {source}"
        else:
            sms_body = f"{title}. {severity_word.upper()}. {area}. {action} - {source}"
        sms_text = fit(sms_body, SMS_LIMIT)
        messages["sms"][language] = {"text": sms_text, **sms_meta(sms_text)}

        # ------------------------------------------------------------ push
        push_title = fit(
            f"{title}" if severity == "unknown" else f"{title} — {severity_word}",
            64,
        )
        messages["push"][language] = {
            "title": push_title,
            "body": fit(f"{area}. {action}", 140),
        }

        # ------------------------------------------------------------ bulletin
        what, translation_meta = _what_happened(alert, language)
        if translation_meta["method"] not in ("verbatim", "none"):
            translation_report[language] = translation_meta

        lines: list[str] = []
        header = title if severity == "unknown" else f"{title} — {severity_word.upper()}"
        lines.append(header)
        lines.append(f"{'Area' if language == 'en' else 'Zona'}: {area}")
        if alert.onset or expires:
            when = " – ".join(part for part in (alert.onset, expires) if part)
            lines.append(f"{'Time' if language == 'en' else 'Horario'}: {when}")
        if what:
            lines.append("")
            lines.append("What is happening:" if language == "en" else "Qué está pasando:")
            lines.append(what)

        immediate = safety["immediate"][:4]
        if immediate:
            lines.append("")
            lines.append("Do this now:" if language == "en" else "Haga esto ahora:")
            for index, item in enumerate(immediate, start=1):
                lines.append(f"{index}. {item[language]}")

        avoid = safety["avoid"][:3]
        if avoid:
            lines.append("")
            lines.append("Avoid:" if language == "en" else "Evite:")
            for item in avoid:
                lines.append(f"- {item[language]}")

        lines.append("")
        lines.append(
            f"Source: {source}" if language == "en" else f"Fuente: {source}"
        )
        messages["bulletin"][language] = "\n".join(lines).strip()

        # ------------------------------------------------------------ voice
        spoken = [
            f"{title}." if language == "en" else f"{title}.",
            f"{'Area' if language == 'en' else 'Zona'}: {area}.",
        ]
        if severity != "unknown":
            spoken.append(
                f"{'Severity' if language == 'en' else 'Severidad'}: {severity_word}."
            )
        for item in safety["immediate"][:2]:
            spoken.append(f"{item[language]}")
        messages["voice"][language] = fit(" ".join(spoken), 480)

    messages["severity"] = severity_block(severity)
    messages["title"] = {
        "en": _title(classification, "en"),
        "es": _title(classification, "es"),
    }
    messages["translation"] = translation_report

    return messages


class CommunicationAgent:
    name = "communication"
    stage = "compose"

    def run(self, alert: NormalizedAlert, classification: dict, safety: dict) -> dict[str, Any]:
        return build(alert, classification, safety)
