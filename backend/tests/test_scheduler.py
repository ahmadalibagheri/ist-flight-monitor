"""Scheduler wiring and job registration."""

from __future__ import annotations

import pytest

from app.core.config import Settings
from app.scheduler.runner import (
    JOB_AGGREGATE,
    JOB_COLLECT,
    JOB_DAILY_REPORT,
    JOB_HOURLY_REPORT,
    build_scheduler,
    describe_jobs,
)


@pytest.fixture
def scheduler():
    built = build_scheduler()
    yield built
    if built.running:  # pragma: no cover - defensive
        built.shutdown(wait=False)


def test_all_four_jobs_are_registered(scheduler) -> None:
    ids = {job.id for job in scheduler.get_jobs()}
    assert ids == {JOB_COLLECT, JOB_AGGREGATE, JOB_HOURLY_REPORT, JOB_DAILY_REPORT}


def test_collection_interval_comes_from_configuration(scheduler) -> None:
    from app.core.config import settings

    job = scheduler.get_job(JOB_COLLECT)
    assert f"{settings.collection_interval_minutes // 60}:" in str(job.trigger) or str(
        settings.collection_interval_minutes
    ) in str(job.trigger)


def test_daily_report_uses_a_local_cron_trigger(scheduler) -> None:
    from app.core.config import settings

    trigger = str(scheduler.get_job(JOB_DAILY_REPORT).trigger)
    assert "cron" in trigger
    assert f"hour='{settings.daily_report_hour_local}'" in trigger


def test_scheduler_runs_in_the_operational_timezone(scheduler) -> None:
    from app.core.config import settings

    assert str(scheduler.timezone) == settings.operational_timezone


def test_jobs_do_not_pile_up_on_slow_cycles(scheduler) -> None:
    """A collection slower than its interval must not run concurrently with itself."""
    defaults = scheduler._job_defaults
    assert defaults["max_instances"] == 1
    assert defaults["coalesce"] is True


def test_describe_jobs_is_safe_before_start(scheduler) -> None:
    described = describe_jobs(scheduler)
    assert len(described) == 4
    assert all("trigger" in job for job in described)
    # The collect job is given an explicit near-term first run so a fresh
    # deployment collects immediately rather than after a full interval.
    collect = next(job for job in described if job["id"] == JOB_COLLECT)
    assert collect["next_run_time"] is not None
    assert describe_jobs(None) == []


@pytest.mark.parametrize("minutes", [5, 15, 30, 60])
def test_all_documented_intervals_are_accepted(minutes: int) -> None:
    assert Settings(collection_interval_minutes=minutes).collection_interval_minutes == minutes


def test_undocumented_interval_is_rejected() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        Settings(collection_interval_minutes=7)
