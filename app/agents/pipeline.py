"""The alert pipeline: Weather -> Safety -> Communication.

Each agent's output is the next agent's input, and every hop is recorded. The
pipeline degrades rather than fails: if an optional cloud stage misbehaves, the
local agent takes that hop and the job still completes.
"""

from __future__ import annotations

import time
from typing import Any

from ..domain.alert import normalize
from .base import PipelineState
from .communication import CommunicationAgent
from .safety import SafetyAgent
from .weather import WeatherAgent

SUPPORTED_LANGUAGES = ("en", "es")


def _pipeline_engine(steps) -> str:
    """Summarise which engines actually served this alert.

    "mixed" used to mean "we were online", which was misleading: an online run
    with Swarm unconfigured is still 100% local, and the dashboard would label
    it as cloud-served.
    """
    engines = {step.engine for step in steps}
    if engines == {"swarm"}:
        return "swarm"
    if "swarm" in engines:
        return "mixed"
    return "local"


class AlertPipeline:
    def __init__(self, cloud=None, languages: tuple[str, ...] = SUPPORTED_LANGUAGES) -> None:
        self.weather = WeatherAgent()
        self.safety = SafetyAgent()
        self.communication = CommunicationAgent()
        self.cloud = cloud
        self.languages = languages

    async def run(
        self,
        raw_alert: dict | None,
        languages: list[str] | None = None,
        audience: str = "public",
    ) -> dict[str, Any]:
        alert = normalize(raw_alert)
        requested = [lang for lang in (languages or self.languages) if lang in SUPPORTED_LANGUAGES]
        state = PipelineState(alert=alert, languages=requested or ["en", "es"], audience=audience)

        if not alert.areas:
            state.warnings.append("alert has no area; messages say 'the affected area'")
        if alert.language not in SUPPORTED_LANGUAGES:
            state.warnings.append(
                f"source language '{alert.language}' is outside the curated set; "
                "free-text translation may be incomplete"
            )

        # ------------------------------------------------------------ weather
        started = time.perf_counter()
        classification = self.weather.run(alert)
        elapsed = (time.perf_counter() - started) * 1000
        state.classification = classification
        state.record(
            self.weather.name,
            self.weather.stage,
            "local",
            elapsed,
            handoff=(
                f"hazard={classification['hazard']} "
                f"severity={classification['severity']} "
                f"confidence={classification['confidence']}"
            ),
            detail={"product": classification.get("product")},
        )

        # ------------------------------------------------------------ safety
        started = time.perf_counter()
        safety_plan = self.safety.run(alert, classification)
        elapsed = (time.perf_counter() - started) * 1000
        state.safety = safety_plan
        state.record(
            self.safety.name,
            self.safety.stage,
            "local",
            elapsed,
            handoff=(
                f"{len(safety_plan['immediate'])} immediate / "
                f"{len(safety_plan['avoid'])} avoid / "
                f"{len(safety_plan['prepare'])} prepare actions, "
                f"tier={safety_plan['tier']}"
            ),
            detail={
                "evacuation": safety_plan["evacuation_recommended"],
                "shelter": safety_plan["shelter_recommended"],
            },
        )

        # ------------------------------------------------------------ communication
        started = time.perf_counter()
        engine = "local"
        # The local composer always runs (milliseconds): it is the fallback and
        # it supplies the curated titles the cloud copy must keep.
        local_messages = self.communication.run(alert, classification, safety_plan)
        messages = None
        cloud_meta: dict[str, Any] = {}

        if self.cloud is not None:
            if hasattr(self.cloud, "compose_with_meta"):
                messages, cloud_meta = await self.cloud.compose_with_meta(
                    alert, classification, safety_plan, local=local_messages
                )
            else:
                messages = await self.cloud.compose(
                    alert, classification, safety_plan, local=local_messages
                )
            if messages is not None:
                engine = "swarm"

        if messages is None:
            messages = local_messages

        elapsed = (time.perf_counter() - started) * 1000
        state.communication = messages
        state.record(
            self.communication.name,
            self.communication.stage,
            engine,
            elapsed,
            handoff=(
                f"sms {messages['sms']['en']['chars']} chars "
                f"({messages['sms']['en']['segments']} segment) + "
                f"sms es {messages['sms']['es']['segments']} segments"
            ),
            detail={
                "channels": ["sms", "push", "bulletin", "voice"],
                "languages": state.languages,
                **({"cloud": cloud_meta} if cloud_meta else {}),
            },
        )

        if engine == "swarm":
            # Cloud-authored copy still needs the deterministic safety block to
            # stay authoritative; note it so the UI is not ambiguous.
            drafted_by = (
                "a Swarm Mixture of Agents ("
                + ", ".join(cloud_meta.get("experts", []))
                + f" -> {cloud_meta.get('aggregator')})"
                if cloud_meta.get("strategy") == "moa"
                else "Swarm"
            )
            state.warnings.append(
                f"communication copy was drafted by {drafted_by}; "
                "safety actions remain locally generated"
            )

        return {
            "alert": alert.to_dict(),
            "classification": classification,
            "safety": safety_plan,
            "communication": messages,
            "trace": [item.to_dict() for item in state.trace],
            "warnings": state.warnings,
            "languages": state.languages,
            "engine": _pipeline_engine(state.trace),
            "offline": state.offline,
            "meta": {
                "agents": [self.weather.name, self.safety.name, self.communication.name],
                "pipeline": "weather->safety->communication",
                "audience": audience,
            },
        }
