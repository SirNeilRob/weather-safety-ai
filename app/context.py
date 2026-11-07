"""Application wiring.

One object owns every long-lived service so tests can build an app with stub
settings and no global state.
"""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from .agents.cloud import CloudDrafter
from .agents.pipeline import AlertPipeline
from .config import Settings
from .core.connectivity import ConnectivityMonitor
from .core.db import JobStore
from .core.noaa import NoaaFeed
from .core.processors.local import build_local_processor
from .core.processors.swarm import SwarmProcessor
from .core.router import SmartRouter
from .core.sync import SyncWorker
from .core.worker import WorkerPool

logger = logging.getLogger(__name__)


class AppContext:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.store = JobStore(settings.db_path)
        self.monitor = ConnectivityMonitor(
            probes=settings.probe_urls,
            interval=settings.probe_interval,
            timeout=settings.probe_timeout,
            offline_after=settings.offline_after_failures,
            online_after=settings.online_after_successes,
            forced_offline=settings.forced_offline,
        )
        self.swarm = SwarmProcessor(
            base_url=settings.swarm_base_url,
            api_key=settings.swarm_api_key,
            endpoint=settings.swarm_endpoint,
            model=settings.swarm_model,
            timeout=settings.swarm_timeout,
            max_retries=settings.swarm_max_retries,
            sync_endpoint=settings.swarm_sync_endpoint,
        )
        self.drafter = CloudDrafter(
            swarm=self.swarm,
            monitor=self.monitor,
            stages=settings.alert_cloud_stages,
            timeout=settings.alert_cloud_timeout,
            cooldown=settings.breaker_cooldown,
        )
        self.alert_pipeline = AlertPipeline(cloud=self.drafter)
        self.local = build_local_processor(
            engine=settings.local_engine,
            ollama_url=settings.ollama_url,
            ollama_model=settings.ollama_model,
            ollama_timeout=settings.ollama_timeout,
            alert_pipeline=self.alert_pipeline,
        )
        self.router = SmartRouter(
            monitor=self.monitor,
            swarm=self.swarm,
            local=self.local,
            breaker_threshold=settings.breaker_threshold,
            breaker_cooldown=settings.breaker_cooldown,
        )
        self.workers = WorkerPool(
            store=self.store,
            router=self.router,
            concurrency=settings.worker_count,
            max_attempts=settings.max_attempts,
            job_timeout=settings.job_timeout,
            idle_poll=settings.worker_idle_poll,
        )
        self.syncer = SyncWorker(
            store=self.store,
            swarm=self.swarm,
            monitor=self.monitor,
            interval=settings.sync_interval,
            batch=settings.sync_batch,
            request_timeout=settings.swarm_timeout,
        )
        self.noaa = None
        if settings.noaa_enabled:
            default_point = None
            if settings.noaa_latitude is not None and settings.noaa_longitude is not None:
                default_point = (settings.noaa_latitude, settings.noaa_longitude)
            self.noaa = NoaaFeed(
                store=self.store,
                monitor=self.monitor,
                wake=self.workers.wake,
                state_path=Path(settings.db_path).parent / "noaa_state.json",
                user_agent=settings.noaa_user_agent,
                interval=settings.noaa_poll_interval,
                default_point=default_point,
            )
        self.started = False

    # ------------------------------------------------------------------ lifecycle
    async def start(self) -> None:
        if self.started:
            return
        self.started = True

        orphaned = await self.store.requeue_stale()
        if orphaned:
            logger.warning("requeued %d job(s) left running by a previous process", orphaned)

        if self.settings.probe_on_startup:
            try:
                await asyncio.wait_for(
                    self.monitor.probe_once(),
                    timeout=min(6.0, max(1.0, self.settings.probe_timeout * 2)),
                )
            except (asyncio.TimeoutError, TimeoutError):
                logger.warning("startup connectivity probe timed out; assuming offline")

        await self.monitor.start()
        await self.workers.start()
        await self.syncer.start()
        if self.noaa is not None:
            await self.noaa.start()
        logger.info("context started (local engine: %s)", self.local.name)

    async def stop(self) -> None:
        if self.noaa is not None:
            await self.noaa.stop()
        await self.syncer.stop()
        await self.workers.stop()
        await self.monitor.stop()
        self.store.close()
        self.started = False

    # ------------------------------------------------------------------ reporting
    def snapshot(self) -> dict:
        return {
            "app": self.settings.app_name,
            "environment": "offline-first",
            "connectivity": self.monitor.snapshot(),
            "swarm": self.swarm.snapshot(),
            "router": self.router.snapshot(),
            "local": {"engine": self.local.name},
            "alert_pipeline": {
                "agents": ["weather", "safety", "communication"],
                "cloud_stages": sorted(self.settings.alert_cloud_stages),
                "cloud_ready": self.drafter.enabled("communication"),
            },
            "sync": {
                "configured": self.swarm.can_sync,
                "last_result": self.syncer.last_result,
                "sent_total": self.syncer.sent,
                "failed_total": self.syncer.failed,
            },
            "workers": {
                "concurrency": self.workers.concurrency,
                "processed": self.workers.processed,
                "failed": self.workers.failed,
            },
        }
