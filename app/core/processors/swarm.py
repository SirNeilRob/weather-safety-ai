"""Swarm API processor.

Deliberately defensive, because this client is the part that runs while the
network is flaky:

* every call has a hard timeout,
* transport errors and 5xx/429 are retried with exponential backoff,
* 4xx (bad key, bad model) fails fast instead of burning the retry budget,
* any failure raises ``ProcessorUnavailable`` so the router can fall back to the
  local engine instead of surfacing a 500 to the user.

The response parser accepts an OpenAI-compatible chat completion and a few
near-misses, so a slightly different Swarm response shape does not require a
code change mid-hackathon.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import httpx

from .base import ProcessorUnavailable, extract_text

logger = logging.getLogger(__name__)

RETRY_STATUS = {408, 425, 429, 500, 502, 503, 504}


def _extract_json(text: str):
    """Pull the first JSON object out of a model reply.

    Models wrap JSON in prose or fences often enough that a bare ``json.loads``
    is not good enough for a reliability-critical path.
    """
    import json as _json

    candidate = text.strip()
    if candidate.startswith("```"):
        candidate = candidate.strip("`")
        if candidate.lower().startswith("json"):
            candidate = candidate[4:]
    try:
        return _json.loads(candidate)
    except _json.JSONDecodeError:
        pass

    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end > start:
        try:
            return _json.loads(text[start : end + 1])
        except _json.JSONDecodeError:
            return None
    return None


class SwarmProcessor:
    name = "swarm"

    def __init__(
        self,
        base_url: str | None,
        api_key: str | None = None,
        endpoint: str = "/v1/chat/completions",
        model: str = "swarm-default",
        timeout: float = 20.0,
        max_retries: int = 2,
        sync_endpoint: str | None = None,
        retry_base: float = 0.5,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = (base_url or "").rstrip("/")
        self.api_key = api_key
        self.endpoint = endpoint if endpoint.startswith("/") else f"/{endpoint}"
        self.model = model
        self.timeout = timeout
        self.max_retries = max(0, int(max_retries))
        self.sync_endpoint = sync_endpoint
        self.retry_base = retry_base
        self._transport = transport
        self.last_error: str | None = None
        self.last_status: int | None = None

    # ------------------------------------------------------------------ config
    @property
    def configured(self) -> bool:
        return bool(self.base_url)

    @property
    def can_sync(self) -> bool:
        return bool(self.base_url and self.sync_endpoint)

    def _url(self, path: str) -> str:
        return f"{self.base_url}{path if path.startswith('/') else '/' + path}"

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return headers

    # ------------------------------------------------------------------ request body
    def _build_body(self, kind: str, payload: dict) -> dict:
        messages = payload.get("messages")
        if not isinstance(messages, list) or not messages:
            prompt = payload.get("prompt") or extract_text(payload)
            if kind and kind not in ("analyze", "analyse", "default", "chat", "complete"):
                prompt = f"[task: {kind}]\n{prompt}"
            messages = [{"role": "user", "content": str(prompt)}]

        body: dict[str, Any] = {
            "model": payload.get("model") or self.model,
            "messages": messages,
            "stream": False,
        }

        for source_key, target_key in (
            ("temperature", "temperature"),
            ("max_tokens", "max_tokens"),
            ("top_p", "top_p"),
            ("response_format", "response_format"),
        ):
            if source_key in payload and payload[source_key] is not None:
                body[target_key] = payload[source_key]

        extra = payload.get("swarm_options")
        if isinstance(extra, dict):
            body.update({k: v for k, v in extra.items() if k not in ("messages", "stream")})
        return body

    # ------------------------------------------------------------------ parsing
    @staticmethod
    def _parse(data: Any) -> dict:
        if isinstance(data, dict):
            choices = data.get("choices")
            if isinstance(choices, list) and choices:
                choice = choices[0] or {}
                message = choice.get("message") or {}
                content = message.get("content")
                if isinstance(content, list):
                    content = " ".join(
                        part.get("text", "")
                        for part in content
                        if isinstance(part, dict)
                    )
                if isinstance(content, str):
                    return {
                        "text": content,
                        "model": data.get("model"),
                        "usage": data.get("usage"),
                        "finish_reason": choice.get("finish_reason"),
                        "engine": "swarm",
                        "offline": False,
                    }
                if isinstance(choice.get("text"), str):
                    return {
                        "text": choice["text"],
                        "model": data.get("model"),
                        "usage": data.get("usage"),
                        "engine": "swarm",
                        "offline": False,
                    }

            if isinstance(data.get("output_text"), str):
                return {
                    "text": data["output_text"],
                    "model": data.get("model"),
                    "engine": "swarm",
                    "offline": False,
                }

        return {"data": data, "engine": "swarm", "offline": False}

    # ------------------------------------------------------------------ calls
    @staticmethod
    def _retry_after(response: httpx.Response) -> float | None:
        raw = response.headers.get("retry-after")
        if not raw:
            return None
        try:
            return min(10.0, max(0.0, float(raw)))
        except ValueError:
            return None

    async def run(self, kind: str, payload: dict) -> dict:
        if not self.configured:
            raise ProcessorUnavailable("SWARM_BASE_URL is not configured")

        body = self._build_body(kind, payload)
        client_kwargs: dict[str, Any] = {"timeout": httpx.Timeout(self.timeout)}
        if self._transport is not None:
            client_kwargs["transport"] = self._transport

        attempt = 0
        while True:
            try:
                async with httpx.AsyncClient(**client_kwargs) as client:
                    response = await client.post(
                        self._url(self.endpoint), json=body, headers=self._headers()
                    )
            except (httpx.TransportError, httpx.TimeoutException, OSError) as exc:
                self.last_error = f"{type(exc).__name__}: {exc}"
                if attempt < self.max_retries:
                    await asyncio.sleep(self.retry_base * (2**attempt))
                    attempt += 1
                    continue
                raise ProcessorUnavailable(self.last_error) from exc

            self.last_status = response.status_code

            if response.status_code in RETRY_STATUS and attempt < self.max_retries:
                delay = self._retry_after(response) or self.retry_base * (2**attempt)
                logger.warning(
                    "swarm %s -> retrying in %.1fs (attempt %d/%d)",
                    response.status_code,
                    delay,
                    attempt + 1,
                    self.max_retries,
                )
                await asyncio.sleep(delay)
                attempt += 1
                continue

            if response.status_code >= 400:
                snippet = response.text[:300].replace("\n", " ")
                self.last_error = f"HTTP {response.status_code}: {snippet}"
                raise ProcessorUnavailable(self.last_error)

            try:
                data = response.json()
            except json.JSONDecodeError as exc:
                self.last_error = "swarm returned non-JSON body"
                raise ProcessorUnavailable(self.last_error) from exc

            self.last_error = None
            return self._parse(data)

    async def push_sync(self, record: dict) -> dict:
        """Push one offline-produced result upstream for reconciliation."""
        if not self.can_sync:
            raise ProcessorUnavailable("SWARM_SYNC_ENDPOINT is not configured")

        client_kwargs: dict[str, Any] = {"timeout": httpx.Timeout(self.timeout)}
        if self._transport is not None:
            client_kwargs["transport"] = self._transport

        async with httpx.AsyncClient(**client_kwargs) as client:
            response = await client.post(
                self._url(self.sync_endpoint or ""),
                json=record,
                headers=self._headers(),
            )
        if response.status_code >= 400:
            raise ProcessorUnavailable(
                f"sync HTTP {response.status_code}: {response.text[:200]}"
            )
        try:
            return response.json()
        except json.JSONDecodeError:
            return {"ok": True}

    # ------------------------------------------------------------------ agent drafting
    async def draft_communication(
        self, alert: dict, classification: dict, safety: dict
    ) -> dict:
        """Ask Swarm to rewrite the four channels in both languages.

        Returns parsed JSON only. Callers must validate the shape before use;
        the pipeline falls back to the local agent on anything unexpected.
        """
        import json as _json

        system = (
            "You are the Communication agent of an emergency alerting system. "
            "You receive a classified weather alert and a verified safety plan. "
            "Rewrite the delivery channels so they are clear and calm. "
            "Do not invent facts, do not change the severity, and do not add "
            "actions that are not in the safety plan. "
            "Return ONLY a JSON object with this exact shape: "
            '{"title": {"en": str, "es": str}, '
            '"sms": {"en": {"text": str}, "es": {"text": str}}, '
            '"push": {"en": {"title": str, "body": str}, "es": {"title": str, "body": str}}, '
            '"bulletin": {"en": str, "es": str}, '
            '"voice": {"en": str, "es": str}}. '
            "Spanish must be natural Spanish, not word-for-word English. "
            "SMS stays under 160 characters."
        )
        user = _json.dumps(
            {
                "alert": alert,
                "classification": {
                    "hazard": classification.get("hazard"),
                    "severity": classification.get("severity"),
                    "product": classification.get("product"),
                },
                "safety": {
                    "immediate": [item["en"] for item in safety.get("immediate", [])],
                    "avoid": [item["en"] for item in safety.get("avoid", [])],
                },
            },
            ensure_ascii=False,
        )

        result = await self.run(
            "communication",
            {
                "messages": [
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                "response_format": {"type": "json_object"},
                "temperature": 0.2,
            },
        )

        text = result.get("text") if isinstance(result, dict) else None
        if not isinstance(text, str) or not text.strip():
            raise ProcessorUnavailable("swarm returned no communication text")

        parsed = _extract_json(text)
        if parsed is None:
            raise ProcessorUnavailable("swarm communication was not valid JSON")
        return parsed

    def snapshot(self) -> dict:
        return {
            "configured": self.configured,
            "base_url": self.base_url or None,
            "endpoint": self.endpoint,
            "model": self.model,
            "sync_configured": self.can_sync,
            "last_status": self.last_status,
            "last_error": self.last_error,
        }
