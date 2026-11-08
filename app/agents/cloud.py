"""Optional Swarm enrichment for a single pipeline stage.

The offline pipeline is complete on its own. When the link is up, the
communication stage can be handed to Swarm to produce more natural wording.
This wrapper exists so that upgrade is *optional and safe*:

* it checks connectivity and configuration before every call,
* it validates the returned JSON against the contract the frontend expects,
* it returns ``None`` on any deviation, which makes the pipeline fall back to the
  local agent for that stage only.

A half-formed cloud response must never reach a user who is deciding whether to
evacuate.
"""

from __future__ import annotations

import logging
from typing import Any

from .communication import severity_block, sms_meta, to_gsm7

logger = logging.getLogger(__name__)

REQUIRED_CHANNELS = ("sms", "push", "bulletin", "voice")
REQUIRED_LANGUAGES = ("en", "es")

#: A model-written SMS over this length is rejected, not silently truncated.
MAX_SMS_CHARS = 160


def _valid(payload: Any) -> bool:
    if not isinstance(payload, dict):
        return False
    for channel in REQUIRED_CHANNELS:
        block = payload.get(channel)
        if not isinstance(block, dict):
            return False
        for language in REQUIRED_LANGUAGES:
            if language not in block:
                return False
    if "title" not in payload or not isinstance(payload["title"], dict):
        return False
    return True


def finish(payload: dict, severity: str) -> dict | None:
    """Attach the parts of the envelope the model must not own.

    The model writes prose. It does not get to describe severity, and it does
    not get to decide how many SMS segments a carrier will bill: both are
    deterministic and both are rendered by the dashboard. Anything that fails
    these checks returns ``None`` and the local composer takes the stage.
    """
    sms = payload["sms"]
    for language in REQUIRED_LANGUAGES:
        block = sms.get(language)
        text = block.get("text") if isinstance(block, dict) else None
        if not isinstance(text, str) or not text.strip():
            logger.warning("cloud SMS for %s was empty; using local", language)
            return None
        if len(text) > MAX_SMS_CHARS:
            logger.warning(
                "cloud SMS for %s was %s chars (limit %s); using local",
                language,
                len(text),
                MAX_SMS_CHARS,
            )
            return None
        # Model prose goes through the same typography fold as the local
        # composer: an em dash from the model must not turn a 150-character
        # message into two carrier segments.
        text = to_gsm7(text)
        sms[language] = {"text": text, **sms_meta(text)}

    for channel in ("bulletin", "voice"):
        for language in REQUIRED_LANGUAGES:
            text = payload[channel].get(language)
            if not isinstance(text, str) or not text.strip():
                logger.warning("cloud %s for %s was empty; using local", channel, language)
                return None

    payload["severity"] = severity_block(severity)
    # Cloud copy is authored in both languages, so there is nothing to report.
    payload["translation"] = {}
    return payload


class CloudDrafter:
    def __init__(self, swarm, monitor, stages: tuple[str, ...] = ("communication",)) -> None:
        self.swarm = swarm
        self.monitor = monitor
        self.stages = frozenset(stages)

    def enabled(self, stage: str) -> bool:
        if stage not in self.stages:
            return False
        if not getattr(self.swarm, "configured", False):
            return False
        return bool(self.monitor.is_online())

    async def compose(self, alert, classification: dict, safety: dict) -> dict | None:
        if not self.enabled("communication"):
            return None
        try:
            payload = await self.swarm.draft_communication(
                alert=alert.to_dict(),
                classification=classification,
                safety=safety,
            )
        except Exception as exc:
            logger.warning("cloud communication stage unavailable: %s", exc)
            return None

        if not _valid(payload):
            logger.warning("cloud communication stage returned an invalid shape; using local")
            return None

        return finish(payload, str(classification.get("severity", "unknown")))
