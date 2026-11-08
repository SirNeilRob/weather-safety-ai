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

from .communication import severity_block, sms_meta, to_gsm7

logger = logging.getLogger(__name__)

STRATEGIES = ("single", "moa")
SEVERITY_RANK = {"unknown": 0, "minor": 1, "moderate": 2, "severe": 3, "extreme": 4}

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
        strategy: str = "single",
        moa_min_severity: str = "minor",
        moa_timeout: float | None = None,
    ) -> None:
        self.swarm = swarm
        self.monitor = monitor
        self.stages = frozenset(stages)
        self.timeout = float(timeout)
        self.cooldown = float(cooldown)
        self._clock = clock
        self._skip_until = 0.0
        self.last_error: str | None = None
        self.strategy = strategy if strategy in STRATEGIES else "single"
        self.moa_min_severity = moa_min_severity if moa_min_severity in SEVERITY_RANK else "minor"
        # MoA is two model hops (experts, then editor); leave room inside the
        # stage budget for a single-agent retry if it fails.
        self.moa_timeout = float(moa_timeout) if moa_timeout else max(5.0, self.timeout * 0.7)
        self.last_moa_error: str | None = None

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

    def use_moa(self, classification: dict) -> bool:
        """MoA costs ~4 model calls; spend it where the stakes justify it."""
        if self.strategy != "moa" or not hasattr(self.swarm, "draft_communication_moa"):
            return False
        severity = str(classification.get("severity", "unknown"))
        return SEVERITY_RANK.get(severity, 0) >= SEVERITY_RANK[self.moa_min_severity]

    def _accept(self, payload: Any, classification: dict, local: dict | None, source: str) -> dict | None:
        """Every cloud draft, single or MoA, passes the same gate."""
        if not _valid(payload):
            logger.warning("cloud %s returned an invalid shape; rejected", source)
            return None
        word = wrong_product_term(payload, classification.get("product"))
        if word:
            logger.warning(
                "cloud %s Spanish copy used %r for a %s; rejected",
                source,
                word,
                classification.get("product"),
            )
            return None
        return finish(payload, str(classification.get("severity", "unknown")), local)

    async def compose(
        self, alert, classification: dict, safety: dict, local: dict | None = None
    ) -> dict | None:
        messages, _meta = await self.compose_with_meta(alert, classification, safety, local)
        return messages

    async def compose_with_meta(
        self, alert, classification: dict, safety: dict, local: dict | None = None
    ) -> tuple[dict | None, dict]:
        """Fallback chain: MoA -> single agent -> (caller uses) local composer.

        Returns ``(messages, meta)``; ``meta`` says which strategy produced the
        copy and why a richer one was skipped, for the dashboard trace.
        """
        if not self.enabled("communication"):
            return None, {}
        kwargs = dict(
            alert=alert.to_dict(),
            classification=classification,
            safety=safety,
            official_title=(local or {}).get("title"),
        )
        started = self._clock()
        meta: dict[str, Any] = {}

        if self.use_moa(classification):
            try:
                payload, moa_meta = await asyncio.wait_for(
                    self.swarm.draft_communication_moa(**kwargs, timeout=self.moa_timeout),
                    timeout=self.moa_timeout,
                )
                accepted = self._accept(payload, classification, local, "MoA")
                if accepted is not None:
                    self.last_error = self.last_moa_error = None
                    return accepted, moa_meta
                self.last_moa_error = "editor output failed validation"
            except (asyncio.TimeoutError, TimeoutError):
                self.last_moa_error = f"no answer within {self.moa_timeout:.0f}s"
            except Exception as exc:
                self.last_moa_error = f"{type(exc).__name__}: {exc}"
            logger.warning("MoA communication failed (%s); trying single agent", self.last_moa_error)
            meta["moa_fallback"] = self.last_moa_error
        elif self.strategy == "moa":
            meta["moa_skipped"] = f"severity below {self.moa_min_severity}"

        remaining = self.timeout - (self._clock() - started)
        if remaining < 3.0:
            self._failed("stage budget spent on MoA")
            return None, meta
        try:
            payload = await asyncio.wait_for(
                self.swarm.draft_communication(**kwargs), timeout=remaining
            )
        except (asyncio.TimeoutError, TimeoutError):
            self._failed(f"no answer within {remaining:.0f}s")
            return None, meta
        except Exception as exc:
            self._failed(f"{type(exc).__name__}: {exc}")
            return None, meta

        accepted = self._accept(payload, classification, local, "single-agent")
        if accepted is None:
            return None, meta
        self.last_error = None
        return accepted, {"strategy": "single", **meta}

    def snapshot(self) -> dict:
        return {
            "stages": sorted(self.stages),
            "strategy": self.strategy,
            "moa_min_severity": self.moa_min_severity if self.strategy == "moa" else None,
            "timeout_seconds": self.timeout,
            "moa_timeout_seconds": self.moa_timeout if self.strategy == "moa" else None,
            "cooling_down": self._clock() < self._skip_until,
            "last_error": self.last_error,
            "last_moa_error": self.last_moa_error,
        }
