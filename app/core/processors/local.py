"""Offline processing.

This module is the reason the app still works with the network cable pulled.
It has two engines:

``LocalProcessor``
    Pure standard library. Deterministic, instant, no model download, no GPU.
    Covers summarise / classify / extract / analyze, which is enough to keep a
    product demo alive when the venue Wi-Fi dies.

``OllamaProcessor``
    Optional. If a local Ollama daemon is running (or the judges' laptop has
    one), it is used instead and still requires zero internet.

Nothing here imports FastAPI or httpx at module import time except the optional
Ollama wrapper, so the offline path has no web dependencies at all.
"""

from __future__ import annotations

import asyncio
import json
import math
import re
from collections import Counter
from typing import Any

from ...agents.pipeline import AlertPipeline
from .base import ProcessorUnavailable, extract_text

ALERT_KINDS = frozenset({"alert", "weather_alert", "weather-alert", "triage", "classify_alert"})

_WORD_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9'_-]*")
_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+|\n{2,}")

STOPWORDS = frozenset(
    """
    a an the and or but if then than that this these those of to in on for with
    as at by from is are was were be been being it its it's we you they he she
    i me my your our their there here what which who whom will would can could
    should do does did done have has had not no yes so such about into over
    under more most other some any all both each few many much very just also
    """.split()
)

EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}")
URL_RE = re.compile(r"https?://[^\s<>\"')\]]+")
DATE_RE = re.compile(
    r"\b(?:\d{4}-\d{2}-\d{2}|\d{1,2}/\d{1,2}/\d{2,4}|"
    r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.?\s+\d{1,2},?\s*\d{4})\b"
)
MONEY_RE = re.compile(r"(?:[$€£₦]|\b(?:USD|EUR|GBP|NGN)\b)\s?\d[\d,]*(?:\.\d+)?")
NUMBER_RE = re.compile(r"\b\d+(?:\.\d+)?\b")


def words(text: str) -> list[str]:
    return _WORD_RE.findall(text)


def content_words(text: str) -> list[str]:
    return [w.lower() for w in words(text) if w.lower() not in STOPWORDS and len(w) > 2]


def split_sentences(text: str) -> list[str]:
    return [s.strip() for s in _SENTENCE_SPLIT.split(text) if s and s.strip()]


def summarize(text: str, max_sentences: int = 3) -> str:
    """Extractive summary: score sentences by term frequency, keep order.

    Frequency is normalised by sentence length so a long rambling sentence does
    not automatically win. This is intentionally simple: it never fails, and on
    short text it degrades to returning the input.
    """
    sentences = split_sentences(text)
    if len(sentences) <= max_sentences:
        return " ".join(sentences).strip()

    frequencies = Counter(content_words(text))
    if not frequencies:
        return " ".join(sentences[:max_sentences]).strip()

    peak = max(frequencies.values())
    weights = {word: count / peak for word, count in frequencies.items()}

    scored: list[tuple[float, int, str]] = []
    for index, sentence in enumerate(sentences):
        tokens = content_words(sentence)
        if not tokens:
            continue
        raw = sum(weights.get(token, 0.0) for token in tokens)
        scored.append((raw / math.sqrt(len(tokens)), index, sentence))

    if not scored:
        return " ".join(sentences[:max_sentences]).strip()

    top = sorted(scored, key=lambda item: (-item[0], item[1]))[:max_sentences]
    return " ".join(sentence for _, _, sentence in sorted(top, key=lambda item: item[1])).strip()


def keywords(text: str, limit: int = 10) -> list[str]:
    """Top terms by frequency, then by first appearance for stable output."""
    counts = Counter(content_words(text))
    if not counts:
        return []
    first_seen: dict[str, int] = {}
    for index, token in enumerate(content_words(text)):
        first_seen.setdefault(token, index)
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], first_seen.get(kv[0], 0)))
    return [word for word, _ in ranked[:limit]]


def entities(text: str, limit: int = 25) -> dict[str, list[str]]:
    def unique(matches: list[str]) -> list[str]:
        seen: dict[str, None] = {}
        for match in matches:
            seen.setdefault(match, None)
        return list(seen)[:limit]

    return {
        "emails": unique(EMAIL_RE.findall(text)),
        "urls": unique(URL_RE.findall(text)),
        "dates": unique(DATE_RE.findall(text)),
        "amounts": unique(MONEY_RE.findall(text)),
        "numbers": unique(NUMBER_RE.findall(text)),
    }


def _label_spec(labels: Any) -> list[tuple[str, list[str]]]:
    if isinstance(labels, dict):
        spec: list[tuple[str, list[str]]] = []
        for label, hints in labels.items():
            if isinstance(hints, str):
                hints = [hints]
            spec.append((str(label), [str(h).lower() for h in hints]))
        return spec
    if isinstance(labels, (list, tuple)):
        return [
            (str(label), [part.lower() for part in str(label).replace("_", " ").split()])
            for label in labels
        ]
    return []


def classify(text: str, labels: Any) -> dict[str, Any]:
    """Keyword-overlap classifier against caller-supplied labels.

    ``labels`` may be ``["billing", "shipping"]`` or
    ``{"billing": ["invoice", "refund"], ...}`` for better accuracy. Returns the
    runner-up as well, because a demo that shows only one number looks fake.
    """
    spec = _label_spec(labels)
    lowered = text.lower()
    tokens = Counter(content_words(text))

    scores: dict[str, float] = {}
    for label, hints in spec:
        score = 0.0
        for hint in hints:
            if not hint:
                continue
            if " " in hint:
                score += 2.0 * lowered.count(hint)
            else:
                score += float(tokens.get(hint, 0))
        scores[label] = score

    total = sum(scores.values())
    if not spec or total <= 0:
        return {"label": None, "confidence": 0.0, "scores": {}}

    ranked = sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))
    best_label, best_score = ranked[0]
    return {
        "label": best_label,
        "confidence": round(best_score / total, 3),
        "scores": {label: round(value / total, 3) for label, value in ranked if value > 0},
    }


def analyze(text: str) -> dict[str, Any]:
    word_count = len(words(text))
    return {
        "summary": summarize(text),
        "keywords": keywords(text),
        "entities": entities(text),
        "stats": {
            "characters": len(text),
            "words": word_count,
            "sentences": len(split_sentences(text)),
            "reading_time_seconds": max(1, round(word_count / 200 * 60)),
        },
    }


class LocalProcessor:
    """Deterministic offline engine. Cannot fail on network, by construction."""

    name = "local:builtin"

    def __init__(self, default_sentences: int = 3, alert_pipeline: AlertPipeline | None = None) -> None:
        self.default_sentences = default_sentences
        self.alert_pipeline = alert_pipeline

    async def run(self, kind: str, payload: dict) -> dict:
        kind = (kind or "analyze").strip().lower()

        if kind in ALERT_KINDS:
            return await self._run_alert(payload)

        text = extract_text(payload)

        if kind == "summarize":
            limit = int(payload.get("max_sentences") or self.default_sentences)
            result: dict[str, Any] = {"summary": summarize(text, max(1, limit))}
        elif kind == "classify":
            result = classify(text, payload.get("labels") or [])
        elif kind == "extract":
            result = {"entities": entities(text)}
        elif kind in ("analyze", "analyse", "default"):
            result = analyze(text)
        else:
            # Unknown job type: still return something useful rather than a 500.
            result = analyze(text)
            result["note"] = f"unknown kind '{kind}', served generic analysis"

        result["engine"] = self.name
        result["offline"] = True
        return result

    async def _run_alert(self, payload: dict) -> dict:
        """Weather -> Safety -> Communication, fully offline by default.

        Accepts either ``{"alert": {...}}`` or a bare alert object, because the
        frontend team will send both at some point this weekend.
        """
        pipeline = self.alert_pipeline or AlertPipeline()

        nested = payload.get("alert")
        raw_alert = nested if isinstance(nested, dict) else payload

        languages = payload.get("languages")
        if not isinstance(languages, list):
            languages = None

        audience = str(payload.get("audience") or "public")
        result = await pipeline.run(raw_alert, languages=languages, audience=audience)

        # Keep the stage-level summary: calling an online-but-unconfigured run
        # "local+swarm" told the dashboard Swarm had served it when it had not.
        stage_engine = result.get("engine", "local")
        result["pipeline_engine"] = stage_engine
        result["engine"] = self.name if stage_engine == "local" else f"{self.name}+swarm"
        return result


class OllamaProcessor:
    """Local LLM through Ollama. Still fully offline.

    Falls back to ``LocalProcessor`` when the daemon is not reachable, so a
    machine without a downloaded model behaves exactly like the builtin engine
    instead of erroring in front of judges.
    """

    name = "local:ollama"

    def __init__(
        self,
        url: str = "http://127.0.0.1:11434",
        model: str = "llama3.2",
        timeout: float = 120.0,
        fallback: LocalProcessor | None = None,
    ) -> None:
        self.url = url.rstrip("/")
        self.model = model
        self.timeout = timeout
        self.fallback = fallback or LocalProcessor()

    async def available(self) -> bool:
        try:
            import httpx

            async with httpx.AsyncClient(timeout=2.0) as client:
                response = await client.get(f"{self.url}/api/tags")
            return response.status_code == 200
        except Exception:
            return False

    async def run(self, kind: str, payload: dict) -> dict:
        text = extract_text(payload)
        messages = payload.get("messages") if isinstance(payload.get("messages"), list) else None
        if messages is None:
            messages = [
                {
                    "role": "system",
                    "content": (
                        "You are the offline processor of a field application. "
                        "Answer concisely and never claim to have network access."
                    ),
                },
                {"role": "user", "content": text},
            ]

        try:
            import httpx

            async with httpx.AsyncClient(timeout=self.timeout) as client:
                response = await client.post(
                    f"{self.url}/api/chat",
                    json={"model": self.model, "messages": messages, "stream": False},
                )
                response.raise_for_status()
                data = response.json()
            content = (data.get("message") or {}).get("content")
            if not content:
                raise ProcessorUnavailable("ollama returned an empty message")
            return {
                "text": content,
                "engine": self.name,
                "model": self.model,
                "offline": True,
            }
        except ProcessorUnavailable:
            raise
        except Exception as exc:
            result = await self.fallback.run(kind, payload)
            result["fallback_reason"] = f"ollama unavailable: {exc}"
            return result


def build_local_processor(
    engine: str = "builtin",
    ollama_url: str = "http://127.0.0.1:11434",
    ollama_model: str = "llama3.2",
    ollama_timeout: float = 120.0,
    alert_pipeline: AlertPipeline | None = None,
):
    if engine == "ollama":
        return OllamaProcessor(
            url=ollama_url,
            model=ollama_model,
            timeout=ollama_timeout,
            fallback=LocalProcessor(alert_pipeline=alert_pipeline),
        )
    return LocalProcessor(alert_pipeline=alert_pipeline)


async def _selftest() -> None:  # pragma: no cover - manual smoke check
    sample = (
        "The generator at the north site failed at 03:12. "
        "Field crew restored power after replacing the fuel pump. "
        "The outage affected 412 homes for 96 minutes. "
        "A follow-up inspection is scheduled for 2026-11-09."
    )
    processor = LocalProcessor()
    result = await processor.run("analyze", {"text": sample})
    print(json.dumps(result, indent=2))


if __name__ == "__main__":  # pragma: no cover
    asyncio.run(_selftest())
