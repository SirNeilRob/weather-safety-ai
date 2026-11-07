"""Local stand-in for the Swarm API.

Run it when the real Swarm key is missing, when the venue Wi-Fi is down, or
when the frontend needs to exercise the online path without spending credits:

    uvicorn scripts.fake_swarm:app --port 8899

Then point the backend at it:

    SWARM_BASE_URL=http://127.0.0.1:8899 \
    SWARM_SYNC_ENDPOINT=/v1/sync/ingest \
    PROBE_URLS=http://127.0.0.1:8899/v1/health \
    uvicorn app.main:app --port 8000

``GET /v1/sync/inbox`` shows what the backend reconciled after coming back
online, which is the fastest way to prove the sync path works.
"""

from __future__ import annotations

from fastapi import FastAPI, Request

app = FastAPI(title="fake-swarm", version="0.1.0")

INBOX: list[dict] = []


@app.get("/v1/health")
async def health() -> dict:
    return {"ok": True, "service": "fake-swarm"}


@app.post("/v1/chat/completions")
async def chat_completions(request: Request) -> dict:
    body = await request.json()
    messages = body.get("messages") or []
    last = messages[-1] if messages else {}
    text = last.get("content", "") if isinstance(last, dict) else str(last)
    return {
        "id": "chatcmpl-fake",
        "object": "chat.completion",
        "model": body.get("model", "swarm-default"),
        "choices": [
            {
                "index": 0,
                "message": {
                    "role": "assistant",
                    "content": (
                        "Swarm stand-in reply. Received "
                        f"{len(text)} characters across {len(messages)} message(s)."
                    ),
                },
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": len(text) // 4, "completion_tokens": 16, "total_tokens": 0},
    }


@app.post("/v1/sync")
@app.post("/v1/sync/ingest")
async def sync_ingest(request: Request) -> dict:
    record = await request.json()
    INBOX.append(record)
    return {"ok": True, "received": len(INBOX)}


@app.get("/v1/sync/inbox")
async def sync_inbox() -> dict:
    return {"count": len(INBOX), "items": INBOX[-20:]}
