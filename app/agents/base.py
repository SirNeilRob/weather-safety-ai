"""Agent contract.

Three agents run in a fixed order and hand structured artefacts to each other:

    WeatherAgent  -> classification  (what is happening, how bad, how sure)
    SafetyAgent   -> safety plan     (what to do, ordered, bilingual)
    CommunicationAgent -> messages   (SMS, push, bulletin, voice; bilingual)

Every stage records a :class:`StageTrace` entry, so the dashboard can show the
handoff chain and the engine that served each hop. That trace is also the proof
that the agents are not one monolithic function: the safety agent genuinely
consumes the weather agent's output, and the communication agent consumes both.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from ..domain.alert import NormalizedAlert

_SENTENCE_SPLIT = re.compile(r"(?<=[.!?])\s+|\n{2,}")


def sentences(text: str, limit: int = 3) -> list[str]:
    parts = [part.strip() for part in _SENTENCE_SPLIT.split(text or "") if part.strip()]
    return parts[:limit]


def fit(text: str, limit: int) -> str:
    """Hard limit a message without cutting a word in half."""
    text = re.sub(r"\s+", " ", text).strip()
    if len(text) <= limit:
        return text
    window = text[: max(0, limit - 1)]
    cut = window.rfind(" ")
    if cut > limit * 0.5:
        window = window[:cut]
    return window.rstrip(" ,;:-") + "…"


@dataclass(slots=True)
class StageTrace:
    agent: str
    stage: str
    engine: str
    ms: float
    handoff: str
    detail: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent": self.agent,
            "stage": self.stage,
            "engine": self.engine,
            "ms": round(self.ms, 2),
            "handoff": self.handoff,
            "detail": self.detail,
        }


@dataclass(slots=True)
class PipelineState:
    """Mutable state carried between agents."""

    alert: NormalizedAlert
    languages: list[str]
    audience: str = "public"
    classification: dict[str, Any] = field(default_factory=dict)
    safety: dict[str, Any] = field(default_factory=dict)
    communication: dict[str, Any] = field(default_factory=dict)
    translations: dict[str, Any] = field(default_factory=dict)
    trace: list[StageTrace] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    engines: list[str] = field(default_factory=list)

    def record(
        self,
        agent: str,
        stage: str,
        engine: str,
        ms: float,
        handoff: str,
        detail: dict[str, Any] | None = None,
    ) -> None:
        self.trace.append(StageTrace(agent, stage, engine, ms, handoff, detail or {}))
        self.engines.append(engine)

    @property
    def offline(self) -> bool:
        return all(engine == "local" for engine in self.engines)
