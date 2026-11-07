from __future__ import annotations

import pytest

from app.config import Settings


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def offline_settings(tmp_path) -> Settings:
    """Settings that behave like a device with no network and no Swarm key."""
    return Settings(
        db_path=tmp_path / "test.db",
        probe_urls=(),
        probe_on_startup=False,
        probe_interval=3600.0,
        forced_offline=True,
        worker_count=2,
        worker_idle_poll=0.05,
        job_timeout=10.0,
        sync_interval=3600.0,
    )
