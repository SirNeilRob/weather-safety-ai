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

import asyncio
import logging
import time
from typing import Any

from .communication import severity_block, sms_meta

logger = logging.getLogger(__name__)

REQUIRED_CHANNELS = ("sms", "push", "bulletin", "voice")
REQUIRED_LANGUAGES = ("en", "es")

#: A model-written SMS over this length is rejected, not silently truncated.
MAX_SMS_CHARS = 160

#: Spanish product words a model may confuse with the official one. Using a
#: lower-level word changes the severity a reader hears ("Advertencia" reads as
#: an advisory, not a warning), so copy containing one is rejected.
CONFUSABLE_ES = {
    "warning": ("advertencia", "vigilancia", "aviso preventivo"),
    "emergency": ("advertencia", "vigilancia", "aviso preventivo"),
    "watch": ("advertencia", "aviso preventivo"),
    "advisory": ("advertencia", "vigilancia"),
}


def _spanish_texts(payload: dict) -> list[str]:
    texts = [payload["title"].get("es")]
    texts.append((payload["sms"].get("es") or {}).get("text"))
    push = payload["push"].get("es") or {}
    texts += [push.get("title"), push.get("body")]
    texts += [payload["bulletin"].get("es"), payload["voice"].get("es")]
    return [text.lower() for text in texts if isinstance(text, str)]


def wrong_product_term(payload: dict, product: str | None) -> str | None:
    """Return the confusable Spanish product word the copy used, if any."""
    for word in CONFUSABLE_ES.get(product or "", ()):
        if any(word in text for text in _spanish_texts(payload)):
            return word
    return None


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


def finish(payload: dict, severity: str, local: dict | None = None) -> dict | None:
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
        sms[language] = {"text": text, **sms_meta(text)}

    for channel in ("bulletin", "voice"):
        for language in REQUIRED_LANGUAGES:
            text = payload[channel].get(language)
            if not isinstance(text, str) or not text.strip():
                logger.warning("cloud %s for %s was empty; using local", channel, language)
                return None

    if local is not None:
        # The alert type and severity words are curated; the model only writes
        # the body text. Titles therefore always come from the local composer.
        payload["title"] = local["title"]
        for language in REQUIRED_LANGUAGES:
            push = payload["push"].get(language)
            local_push = local["push"].get(language) or {}
            if isinstance(push, dict) and local_push.get("title"):
                push["title"] = local_push["title"]

    payload["severity"] = severity_block(severity)
    # Cloud copy is authored in both languages, so there is nothing to report.
    payload["translation"] = {}
    return payload


class CloudDrafter:
    """Hands the communication stage to Swarm when it is safe to.

    Two guards keep an alert from ever waiting on a bad link: every call is
    capped at ``timeout`` seconds, and after a failure the stage stays local for
    ``cooldown`` seconds instead of paying the timeout again on every alert.
    """

    def __init__(
        self,
        swarm,
        monitor,
        stages: tuple[str, ...] = ("communication",),
        timeout: float = 15.0,
        cooldown: float = 30.0,
        clock=time.monotonic,
    ) -> None:
        self.swarm = swarm
        self.monitor = monitor
        self.stages = frozenset(stages)
        self.timeout = float(timeout)
        self.cooldown = float(cooldown)
        self._clock = clock
        self._skip_until = 0.0
        self.last_error: str | None = None

    def enabled(self, stage: str) -> bool:
        if stage not in self.stages:
            return False
        if not getattr(self.swarm, "configured", False):
            return False
        if self._clock() < self._skip_until:
            return False
        return bool(self.monitor.is_online())

    def _failed(self, reason: str) -> None:
        self.last_error = reason
        self._skip_until = self._clock() + self.cooldown
        logger.warning("cloud communication stage unavailable: %s", reason)

    async def compose(
        self, alert, classification: dict, safety: dict, local: dict | None = None
    ) -> dict | None:
        if not self.enabled("communication"):
            return None
        try:
            payload = await asyncio.wait_for(
                self.swarm.draft_communication(
                    alert=alert.to_dict(),
                    classification=classification,
                    safety=safety,
                    official_title=(local or {}).get("title"),
                ),
                timeout=self.timeout,
            )
        except (asyncio.TimeoutError, TimeoutError):
            self._failed(f"no answer within {self.timeout:.0f}s")
            return None
        except Exception as exc:
            self._failed(f"{type(exc).__name__}: {exc}")
            return None

        if not _valid(payload):
            logger.warning("cloud communication stage returned an invalid shape; using local")
            return None

        word = wrong_product_term(payload, classification.get("product"))
        if word:
            logger.warning(
                "cloud Spanish copy used %r for a %s; using local",
                word,
                classification.get("product"),
            )
            return None

        self.last_error = None
        return finish(payload, str(classification.get("severity", "unknown")), local)

    def snapshot(self) -> dict:
        return {
            "stages": sorted(self.stages),
            "timeout_seconds": self.timeout,
            "cooling_down": self._clock() < self._skip_until,
            "last_error": self.last_error,
        }
