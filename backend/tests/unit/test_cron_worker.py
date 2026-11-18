"""Scheduled jobs use their own queue and one stable id."""

import os

import pytest
from arq import create_pool
from arq.connections import RedisSettings
from arq.constants import job_key_prefix

from src.workers.cron_worker import CRON_QUEUE_NAME, CronWorkerSettings
from src.workers.run_workers import CRON_WORKER, VIDEO_WORKER, embed_cron_worker, worker_settings
from src.workers.tasks import WorkerSettings


def test_cron_jobs_are_not_on_the_video_worker():
    assert WorkerSettings.cron_jobs == []
    assert WorkerSettings.queue_name == "supoclip_tasks"
    assert CronWorkerSettings.queue_name == CRON_QUEUE_NAME
    assert CronWorkerSettings.queue_name != WorkerSettings.queue_name
    jobs = CronWorkerSettings.cron_jobs
    assert len(jobs) == 8
    assert [job.job_id for job in jobs] == [job.name for job in jobs]
    assert {job.job_id for job in jobs} == {
        "cron:check_media_storage",
        "cron:replicate_pending_media",
        "cron:cleanup_media",
        "cron:sweep_orphaned_media",
        "cron:copy_pending_uploads",
        "cron:dispatch_scheduled_social_posts",
        "cron:refresh_social_metrics",
        "cron:process_cancellation_reviews",
    }
    for job in jobs:
        assert job.job_id.count(":") == 1
        assert job.coroutine.__name__ == job.job_id.split(":", 1)[1]


def test_the_worker_process_embeds_cron_unless_asked_not_to():
    assert embed_cron_worker("0") is False
    assert embed_cron_worker("false") is False
    assert embed_cron_worker("1") is True
    assert worker_settings(False) == [VIDEO_WORKER]
    assert worker_settings(True) == [VIDEO_WORKER, CRON_WORKER]


@pytest.mark.asyncio
async def test_a_pending_cron_id_is_not_enqueued_twice():
    redis = await create_pool(RedisSettings(
        host=os.getenv("REDIS_HOST", "127.0.0.1"),
        port=int(os.getenv("REDIS_PORT", "6379")),
    ))
    job_id = "cron:dedupe_probe"
    try:
        first = await redis.enqueue_job(
            "cleanup_media", _job_id=job_id, _queue_name=CRON_QUEUE_NAME
        )
        second = await redis.enqueue_job(
            "cleanup_media", _job_id=job_id, _queue_name=CRON_QUEUE_NAME
        )
        assert first is not None
        assert second is None
        assert await redis.zscore(CRON_QUEUE_NAME, job_id) is not None
        # The video queue is not where this job sits.
        assert await redis.zscore("supoclip_tasks", job_id) is None
    finally:
        await redis.delete(job_key_prefix + job_id)
        await redis.zrem(CRON_QUEUE_NAME, job_id)
        await redis.aclose()


@pytest.mark.asyncio
async def test_a_cron_run_cut_off_by_a_restart_does_not_block_the_next_tick():
    """arq fails an interrupted job with the worker's keep_result, not the cron's."""
    from arq.constants import result_key_prefix
    from arq.worker import Worker

    redis = await create_pool(RedisSettings(
        host=os.getenv("REDIS_HOST", "127.0.0.1"),
        port=int(os.getenv("REDIS_PORT", "6379")),
    ))
    queue = "supoclip_cron_restart_probe"
    job_id = "cron:restart_probe"
    worker = Worker(
        functions=CronWorkerSettings.functions,
        cron_jobs=CronWorkerSettings.cron_jobs,
        queue_name=queue,
        redis_pool=redis,
        keep_result=CronWorkerSettings.keep_result,
    )
    try:
        # What the worker does with a job whose run was interrupted.
        await worker.finish_failed_job(job_id, b"max 1 retries exceeded")
        assert await redis.exists(result_key_prefix + job_id) == 0
        assert await redis.enqueue_job("cleanup_media", _job_id=job_id, _queue_name=queue) is not None
    finally:
        await redis.delete(job_key_prefix + job_id, result_key_prefix + job_id)
        await redis.zrem(queue, job_id)
        await redis.aclose()
