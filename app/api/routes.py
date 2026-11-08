"""HTTP surface.

Contract the frontend codes against:

    POST /v1/alerts              submit a weather alert, get a job back
    GET  /v1/hazards             legend: severity levels, hazards, languages
    POST /v1/jobs                generic job (alert, summarize, classify, extract)
    GET  /v1/jobs/{id}           poll status / result
    GET  /v1/jobs                recent jobs
    GET  /v1/health              link state, queue depth, sync backlog
    POST /v1/demo/offline        force the offline path on/off
    POST /v1/sync/run            force one reconciliation pass

Offline is a first-class state, not an error: ``GET /v1/health`` returns 200
with ``connectivity.state == "offline"``.
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from ..core.db import FAILED, PENDING, RUNNING, STATUSES, Job
from ..core.noaa import WatchlistError
from ..knowledge.hazards import HAZARDS, PRODUCT_TYPES, SEVERITY_LABELS

router = APIRouter(prefix="/v1")

MAX_PAYLOAD_BYTES = 1_000_000
SUPPORTED_LANGUAGES = ("en", "es")
ALERT_KIND = "alert"


# ---------------------------------------------------------------------- schemas
class JobCreateRequest(BaseModel):
    kind: str = "default"
    payload: dict[str, Any] = Field(default_factory=dict)
    #: Serve this job locally even while the link is up (A/B demos, debugging).
    prefer_local: bool = False
    wait_seconds: float = Field(default=0.0, ge=0, le=120)


class AlertCreateRequest(BaseModel):
    alert: dict[str, Any]
    languages: list[str] = Field(default_factory=lambda: list(SUPPORTED_LANGUAGES))
    audience: str = "public"
    wait_seconds: float = Field(default=25.0, ge=0, le=120)


class JobResponse(BaseModel):
    id: str
    kind: str
    status: str
    engine: str | None
    result: dict[str, Any] | None
    error: str | None
    attempts: int
    sync_state: str
    sync_note: str | None
    created_at: str
    updated_at: str
    finished_at: str | None

    @classmethod
    def from_job(cls, job: Job) -> "JobResponse":
        return cls(
            id=job.id,
            kind=job.kind,
            status=job.status,
            engine=job.mode,
            result=job.result,
            error=job.error,
            attempts=job.attempts,
            sync_state=job.sync_state,
            sync_note=job.sync_note,
            created_at=job.created_at,
            updated_at=job.updated_at,
            finished_at=job.finished_at,
        )


class OfflineToggle(BaseModel):
    enabled: bool


class WatchZipRequest(BaseModel):
    zip: str


class WatchPointRequest(BaseModel):
    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)
    name: str


# ---------------------------------------------------------------------- helpers
def _ctx(request: Request):
    ctx = getattr(request.app.state, "ctx", None)
    if ctx is None:  # pragma: no cover - wiring bug, not a user error
        raise HTTPException(status_code=503, detail="application context not ready")
    return ctx


async def _wait_for(store, job_id: str, timeout: float) -> Job | None:
    deadline = time.monotonic() + timeout
    while True:
        job = await store.get(job_id)
        if job is None or job.is_terminal or time.monotonic() >= deadline:
            return job
        await asyncio.sleep(0.05)


def _validate_kind(kind: str) -> str:
    clean = (kind or "default").strip()
    if not clean or len(clean) > 64:
        raise HTTPException(status_code=422, detail="kind must be 1-64 characters")
    return clean


def _check_size(payload: dict[str, Any]) -> None:
    size = len(json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8"))
    if size > MAX_PAYLOAD_BYTES:
        raise HTTPException(status_code=413, detail="payload too large")


def _languages(values: list[str] | None) -> list[str]:
    if not values:
        return list(SUPPORTED_LANGUAGES)
    chosen = [value.strip().lower() for value in values if value.strip()]
    valid = [value for value in chosen if value in SUPPORTED_LANGUAGES]
    if not valid:
        raise HTTPException(
            status_code=422,
            detail=f"languages must contain one of: {', '.join(SUPPORTED_LANGUAGES)}",
        )
    return valid


# ---------------------------------------------------------------------- alerts
@router.post("/alerts", response_model=JobResponse, status_code=201)
async def create_alert(body: AlertCreateRequest, request: Request) -> JobResponse:
    """Submit a weather alert.

    Returns a durable job id immediately, or the finished result when
    ``wait_seconds`` is given (default 25s, which is plenty for the local
    pipeline and long enough for one Swarm round trip).
    """
    ctx = _ctx(request)
    if not isinstance(body.alert, dict) or not body.alert:
        raise HTTPException(status_code=422, detail="alert must be a non-empty object")

    languages = _languages(body.languages)
    payload = {
        "alert": body.alert,
        "languages": languages,
        "audience": body.audience or "public",
    }
    _check_size(payload)

    job = await ctx.store.create(ALERT_KIND, payload)
    ctx.workers.wake()

    if body.wait_seconds > 0:
        finished = await _wait_for(ctx.store, job.id, body.wait_seconds)
        if finished is not None:
            return JobResponse.from_job(finished)

    current = await ctx.store.get(job.id)
    return JobResponse.from_job(current or job)


def _expired(result: dict | None, now: datetime) -> bool:
    expires = ((result or {}).get("alert") or {}).get("expires")
    if not expires:
        return False
    try:
        moment = datetime.fromisoformat(str(expires).replace("Z", "+00:00"))
    except ValueError:
        return False
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment <= now


@router.get("/alerts/active")
async def active_alerts(
    request: Request,
    limit: int = Query(default=20, ge=1, le=100),
) -> dict:
    """Processed alerts the dashboard should show, newest first.

    Served from SQLite, so it answers identically online and offline. Alerts
    whose ``expires`` time has passed are left out.
    """
    ctx = _ctx(request)
    now = datetime.now(timezone.utc)
    jobs = await ctx.store.list(limit=200, status="done")
    noaa = ctx.noaa

    def still_issued(job: Job) -> bool:
        # NOAA alerts are shown only while NWS still lists them as active
        # (updates and cancellations replace them); offline, the last list wins.
        if noaa is None or (job.payload or {}).get("source") != "noaa":
            return True
        return noaa.is_current(((job.payload or {}).get("alert") or {}).get("identifier"))

    def with_cities(job: Job) -> dict:
        # Which watched cities this alert covers (one card, every city listed).
        item = JobResponse.from_job(job).model_dump()
        identifier = ((job.payload or {}).get("alert") or {}).get("identifier")
        item["cities"] = noaa.cities_for(identifier) if noaa is not None else []
        return item

    alerts = [
        with_cities(job)
        for job in jobs
        if job.kind == ALERT_KIND
        and job.result
        and not _expired(job.result, now)
        and still_issued(job)
    ][:limit]
    return {"count": len(alerts), "alerts": alerts}


def _noaa(request: Request):
    ctx = _ctx(request)
    if ctx.noaa is None:
        raise HTTPException(status_code=404, detail="NOAA feed is disabled (NOAA_ENABLED=false)")
    return ctx.noaa


@router.get("/noaa")
async def noaa_status(request: Request) -> dict:
    """Watch list, last poll, active NOAA alert count and one observation per city."""
    return _noaa(request).snapshot()


@router.get("/watchlist")
async def watchlist(request: Request) -> dict:
    feed = _noaa(request)
    return {"cities": feed.watchlist(), "max_cities": feed.max_cities}


@router.post("/watchlist", status_code=201)
async def watchlist_add_zip(body: WatchZipRequest, request: Request) -> dict:
    """Add a Florida ZIP (Census ZCTA lookup, city name from NWS /points)."""
    feed = _noaa(request)
    try:
        city = await feed.add_zip(body.zip)
    except WatchlistError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.detail) from exc
    return {"city": city, "cities": feed.watchlist()}


@router.post("/watchlist/point", status_code=201)
async def watchlist_add_point(body: WatchPointRequest, request: Request) -> dict:
    """Add a point inside an NWS alert area (Browse Florida); named by its areaDesc."""
    feed = _noaa(request)
    try:
        city = feed.add_point(body.latitude, body.longitude, body.name)
    except WatchlistError as exc:
        raise HTTPException(status_code=exc.status, detail=exc.detail) from exc
    return {"city": city, "cities": feed.watchlist()}


@router.delete("/watchlist/{key}")
async def watchlist_remove(key: str, request: Request) -> dict:
    feed = _noaa(request)
    if not feed.remove(key):
        raise HTTPException(status_code=404, detail=f"{key} is not on the watch list")
    return {"ok": True, "cities": feed.watchlist()}


@router.post("/noaa/poll")
async def noaa_poll(request: Request) -> dict:
    """Fetch NOAA alerts and conditions now instead of waiting for the timer."""
    return await _noaa(request).poll_once()


@router.get("/hazards")
async def hazard_legend() -> dict:
    """Everything the dashboard needs for its legend and badges."""
    return {
        "severity_levels": [
            {
                "key": key,
                "en": meta["en"],
                "es": meta["es"],
                "color": meta["color"],
                "rank": meta["rank"],
                "advice": {"en": meta["advice"], "es": meta["consejo"]},
            }
            for key, meta in SEVERITY_LABELS.items()
        ],
        "hazards": [
            {"key": hazard.key, "en": hazard.en, "es": hazard.es, "icon": hazard.icon}
            for hazard in HAZARDS.values()
        ],
        "products": [
            {"key": key, "en": meta["en"], "es": meta["es"]}
            for key, meta in PRODUCT_TYPES.items()
        ],
        "languages": [
            {"code": "en", "label": "English"},
            {"code": "es", "label": "Español"},
        ],
        "channels": ["sms", "push", "bulletin", "voice"],
    }


# ---------------------------------------------------------------------- health
@router.get("/health")
async def health(request: Request) -> dict:
    ctx = _ctx(request)
    stats = await ctx.store.stats()
    return {
        "ok": True,
        "service": ctx.settings.app_name,
        "mode": "offline" if not ctx.monitor.is_online() else "online",
        "connectivity": ctx.monitor.snapshot(),
        "swarm": ctx.swarm.snapshot(),
        "router": ctx.router.snapshot(),
        "local": {"engine": ctx.local.name},
        "alert_pipeline": {
            "agents": ["weather", "safety", "communication"],
            "cloud_stages": sorted(ctx.settings.alert_cloud_stages),
            "cloud_ready": ctx.drafter.enabled("communication"),
            "cloud": ctx.drafter.snapshot(),
        },
        "queue": stats,
        "noaa": (
            {k: v for k, v in ctx.noaa.snapshot().items() if k != "cities"}
            | {"cities": len(ctx.noaa.cities)}
            if ctx.noaa is not None
            else {"enabled": False}
        ),
        "sync": {
            "configured": ctx.swarm.can_sync,
            "last_result": ctx.syncer.last_result,
            "sent_total": ctx.syncer.sent,
        },
        "workers": {
            "concurrency": ctx.workers.concurrency,
            "processed": ctx.workers.processed,
            "failed": ctx.workers.failed,
        },
    }


# ---------------------------------------------------------------------- jobs
@router.post("/jobs", response_model=JobResponse, status_code=201)
async def create_job(body: JobCreateRequest, request: Request) -> JobResponse:
    ctx = _ctx(request)
    kind = _validate_kind(body.kind)
    _check_size(body.payload)

    payload = dict(body.payload)
    if body.prefer_local:
        payload["_prefer_local"] = True

    job = await ctx.store.create(kind, payload)
    ctx.workers.wake()

    if body.wait_seconds > 0:
        finished = await _wait_for(ctx.store, job.id, body.wait_seconds)
        if finished is not None:
            return JobResponse.from_job(finished)

    current = await ctx.store.get(job.id)
    return JobResponse.from_job(current or job)


@router.get("/jobs", response_model=list[JobResponse])
async def list_jobs(
    request: Request,
    limit: int = Query(default=25, ge=1, le=200),
    status: str | None = Query(default=None),
) -> list[JobResponse]:
    ctx = _ctx(request)
    if status is not None and status not in STATUSES:
        raise HTTPException(
            status_code=422, detail=f"status must be one of {', '.join(STATUSES)}"
        )
    jobs = await ctx.store.list(limit=limit, status=status)
    return [JobResponse.from_job(job) for job in jobs]


@router.get("/queue/summary")
async def queue_stats(request: Request) -> dict:
    ctx = _ctx(request)
    stats = await ctx.store.stats()
    jobs = await ctx.store.list(limit=5)
    return {
        "queue": stats,
        "recent": [JobResponse.from_job(job).model_dump() for job in jobs],
    }


@router.get("/jobs/{job_id}", response_model=JobResponse)
async def get_job(
    job_id: str,
    request: Request,
    wait: float = Query(default=0.0, ge=0, le=120),
) -> JobResponse:
    ctx = _ctx(request)
    job = await _wait_for(ctx.store, job_id, wait) if wait > 0 else await ctx.store.get(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail="job not found")
    return JobResponse.from_job(job)


# ---------------------------------------------------------------------- demo / ops
@router.post("/demo/offline")
async def demo_offline(body: OfflineToggle, request: Request) -> dict:
    """Force the offline path without touching the network.

    Useful on stage, and useful for the frontend team: they can test the offline
    UI on a machine with perfect Wi-Fi.
    """
    ctx = _ctx(request)
    ctx.monitor.set_forced_offline(body.enabled)
    return {
        "ok": True,
        "forced_offline": ctx.monitor.forced_offline,
        "connectivity": ctx.monitor.snapshot(),
    }


@router.post("/sync/run")
async def run_sync(request: Request) -> dict:
    ctx = _ctx(request)
    return await ctx.syncer.run_once()


@router.get("/capabilities")
async def capabilities(request: Request) -> dict:
    """What the frontend may safely show in the UI."""
    ctx = _ctx(request)
    return {
        "engines": {
            "swarm": ctx.swarm.configured,
            "local": True,
            "local_llm": ctx.local.name == "local:ollama",
        },
        "kinds": [
            ALERT_KIND, "analyze", "summarize", "classify", "extract",
        ],
        "alert_agents": ["weather", "safety", "communication"],
        "languages": list(SUPPORTED_LANGUAGES),
        "channels": ["sms", "push", "bulletin", "voice"],
        "offline_capable": True,
        "sync_on_reconnect": ctx.swarm.can_sync,
        "statuses": list(STATUSES),
        "pending": PENDING,
        "running": RUNNING,
        "failed": FAILED,
    }
