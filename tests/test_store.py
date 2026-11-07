from __future__ import annotations

import asyncio

import pytest

from app.core.db import (
    DONE,
    FAILED,
    PENDING,
    RUNNING,
    SYNC_NOT_NEEDED,
    SYNC_PENDING,
    JobStore,
)

TICK = 0.01  # keep created_at values distinct: the queue orders by timestamp


@pytest.fixture
def store(tmp_path):
    db = JobStore(tmp_path / "jobs.db")
    yield db
    db.close()


@pytest.mark.anyio
async def test_full_lifecycle(store) -> None:
    job = await store.create("summarize", {"text": "hello"})
    assert job.status == PENDING
    assert job.attempts == 0

    claimed = await store.claim_next()
    assert claimed is not None
    assert claimed.id == job.id
    assert claimed.status == RUNNING
    assert claimed.attempts == 1

    # nothing left to claim
    assert await store.claim_next() is None

    finished = await store.finish(
        job.id, {"summary": "hi"}, mode="local", sync_state=SYNC_PENDING, sync_note="offline"
    )
    assert finished is not None
    assert finished.status == DONE
    assert finished.result == {"summary": "hi"}
    assert finished.mode == "local"
    assert finished.sync_state == SYNC_PENDING
    assert finished.finished_at is not None


@pytest.mark.anyio
async def test_failure_can_requeue_or_stop(store) -> None:
    job = await store.create("x", {})
    await store.claim_next()

    requeued = await store.fail(job.id, "transient", retry=True)
    assert requeued is not None
    assert requeued.status == PENDING
    assert requeued.started_at is None

    claimed = await store.claim_next()
    assert claimed is not None
    assert claimed.attempts == 2

    failed = await store.fail(job.id, "permanent", retry=False)
    assert failed is not None
    assert failed.status == FAILED
    assert failed.error == "permanent"
    assert failed.finished_at is not None


@pytest.mark.anyio
async def test_requeue_stale_recovers_crashed_jobs(store) -> None:
    job = await store.create("x", {})
    await store.claim_next()

    recovered = await store.requeue_stale()
    assert recovered == 1

    again = await store.get(job.id)
    assert again is not None
    assert again.status == PENDING
    # attempt count survives so a crash loop still reaches max_attempts
    assert again.attempts == 1


@pytest.mark.anyio
async def test_stats_and_pending_sync(store) -> None:
    first = await store.create("x", {})
    await asyncio.sleep(TICK)
    second = await store.create("x", {})
    await asyncio.sleep(TICK)
    await store.create("x", {})

    claimed = await store.claim_next()
    assert claimed is not None and claimed.id == first.id
    await store.finish(first.id, {"ok": True}, mode="local", sync_state=SYNC_PENDING)

    stats = await store.stats()
    assert stats["total"] == 3
    assert stats["done"] == 1
    assert stats["pending"] == 2
    assert stats["pending_sync"] == 1

    pending = await store.pending_sync(10)
    assert [job.id for job in pending] == [first.id]

    # only jobs that actually need sync show up in the backlog
    await store.set_sync_state(second.id, "sent")
    assert [job.id for job in await store.pending_sync(10)] == [first.id]

    await store.set_sync_state(first.id, "sent")
    assert await store.pending_sync(10) == []


@pytest.mark.anyio
async def test_list_filters_by_status(store) -> None:
    first = await store.create("x", {})
    await asyncio.sleep(TICK)
    await store.create("x", {})

    claimed = await store.claim_next()
    assert claimed is not None and claimed.id == first.id
    await store.finish(first.id, {"ok": True}, mode="swarm", sync_state=SYNC_NOT_NEEDED)

    done = await store.list(status=DONE)
    pending = await store.list(status=PENDING)
    assert len(done) == 1
    assert len(pending) == 1
    assert done[0].mode == "swarm"
