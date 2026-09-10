"""APScheduler process.

Run as ``python -m app.scheduler.runner``. Exactly one instance should run per
deployment - the compose file gives the job to the ``worker`` service and sets
``SCHEDULER_ENABLED=false`` on the API so jobs never fire twice.

All triggers are expressed in the operational timezone, so "the daily report at
23:00" means 23:00 in Istanbul regardless of the host clock.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
from typing import Any

from apscheduler.executors.asyncio import AsyncIOExecutor
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from app.core.config import settings
from app.core.logging_config import configure_logging, get_logger
from app.scheduler.jobs import (
    aggregate_job,
    collect_job,
    daily_report_job,
    hourly_report_job,
    shutdown,
)

log = get_logger(__name__)

JOB_COLLECT = "collect_flights"
JOB_AGGREGATE = "aggregate_statistics"
JOB_HOURLY_REPORT = "hourly_report"
JOB_DAILY_REPORT = "daily_report"


def build_scheduler() -> AsyncIOScheduler:
    """Wire every job onto a scheduler without starting it."""
    scheduler = AsyncIOScheduler(
        executors={"default": AsyncIOExecutor()},
        job_defaults={
            # A slow cycle must not pile up behind itself.
            "coalesce": True,
            "max_instances": 1,
            "misfire_grace_time": 300,
        },
        timezone=settings.tz,
    )

    scheduler.add_job(
        collect_job,
        trigger=IntervalTrigger(minutes=settings.collection_interval_minutes),
        id=JOB_COLLECT,
        name="Collect flight data",
        replace_existing=True,
        # Fire shortly after start so a fresh deployment has data immediately
        # instead of waiting a full interval.
        next_run_time=_soon(),
    )
    scheduler.add_job(
        aggregate_job,
        trigger=IntervalTrigger(minutes=settings.aggregation_interval_minutes),
        id=JOB_AGGREGATE,
        name="Aggregate statistics",
        replace_existing=True,
    )
    scheduler.add_job(
        hourly_report_job,
        trigger=IntervalTrigger(minutes=settings.report_interval_minutes),
        id=JOB_HOURLY_REPORT,
        name="Hourly report",
        replace_existing=True,
    )
    scheduler.add_job(
        daily_report_job,
        trigger=CronTrigger(
            hour=settings.daily_report_hour_local, minute=0, timezone=settings.tz
        ),
        id=JOB_DAILY_REPORT,
        name="Daily historical report",
        replace_existing=True,
    )
    return scheduler


def describe_jobs(scheduler: AsyncIOScheduler | None) -> list[dict[str, Any]]:
    """Job inventory for the ``/health/scheduler`` endpoint."""
    if scheduler is None:
        return []
    jobs = []
    for job in scheduler.get_jobs():
        # Jobs added to a scheduler that has not started yet are still "pending"
        # and carry no next_run_time attribute at all.
        next_run = getattr(job, "next_run_time", None)
        jobs.append(
            {
                "id": job.id,
                "name": job.name,
                "trigger": str(job.trigger),
                "next_run_time": next_run.isoformat() if next_run else None,
            }
        )
    return jobs


def _soon() -> Any:
    from datetime import timedelta

    from app.core.timeutil import utcnow

    return utcnow() + timedelta(seconds=10)


async def _run() -> None:
    configure_logging()
    if not settings.scheduler_enabled:
        log.warning("scheduler.disabled", reason="SCHEDULER_ENABLED=false")
        return

    scheduler = build_scheduler()
    scheduler.start()
    log.info(
        "scheduler.started",
        collection_interval_minutes=settings.collection_interval_minutes,
        aggregation_interval_minutes=settings.aggregation_interval_minutes,
        report_interval_minutes=settings.report_interval_minutes,
        daily_report_hour_local=settings.daily_report_hour_local,
        timezone=settings.operational_timezone,
        jobs=[job["id"] for job in describe_jobs(scheduler)],
    )

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        # Signal handlers are unavailable on non-POSIX loops; the scheduler still
        # runs there, it just relies on the process being killed outright.
        with contextlib.suppress(NotImplementedError):
            loop.add_signal_handler(sig, stop.set)

    try:
        await stop.wait()
    finally:
        log.info("scheduler.stopping")
        scheduler.shutdown(wait=True)
        await shutdown()
        log.info("scheduler.stopped")


def main() -> None:
    asyncio.run(_run())


if __name__ == "__main__":
    main()
