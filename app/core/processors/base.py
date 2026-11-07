"""Processor contract.

A processor turns ``(kind, payload)`` into a JSON-serialisable result. Both the
Swarm client and the offline engine implement the same two members, which is
why the router can swap them without the rest of the app knowing.
"""

from __future__ import annotations


class ProcessorError(RuntimeError):
    """Base class for anything a processor raises on purpose."""


class ProcessorUnavailable(ProcessorError):
    """The processor cannot be used right now (network, quota, config)."""


class Processor:
    """Structural contract; both engines satisfy it without inheriting."""

    name: str

    async def run(self, kind: str, payload: dict) -> dict:  # pragma: no cover
        raise NotImplementedError


def extract_text(payload: dict) -> str:
    """Pull the human-meaningful text out of whatever the client sent.

    Supports the common shapes so the frontend does not have to be rewritten
    per processor: ``text``, ``input``, ``content``, ``prompt``, ``message``,
    or an OpenAI-style ``messages`` array.
    """
    for key in ("text", "input", "content", "prompt", "message"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value

    messages = payload.get("messages")
    if isinstance(messages, list) and messages:
        last = messages[-1]
        if isinstance(last, dict):
            content = last.get("content")
            if isinstance(content, str) and content.strip():
                return content
            if isinstance(content, list):  # OpenAI content parts
                parts = [
                    part.get("text", "")
                    for part in content
                    if isinstance(part, dict) and isinstance(part.get("text"), str)
                ]
                joined = " ".join(p for p in parts if p.strip())
                if joined.strip():
                    return joined

    import json

    return json.dumps(payload, ensure_ascii=False)
