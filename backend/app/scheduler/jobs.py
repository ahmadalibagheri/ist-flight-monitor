"""Scheduled jobs.

Each job owns its own database session and swallows nothing silently: a failure
is logged with context and re-raised only where APScheduler's retry semantics
would help. A collection failure must never take the scheduler process down,
because the next cycle is usually the fix.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.analytics.aggregator import aggregate_recent
from app.analytics.queries import local_today
from app.core.config import settings
from app.core.db import session_scope
from app.core.enums import AlertType
from app.core.logging_config import get_logger
from app.core.timeutil import local_day_bounds, utcnow
from app.models import Flight
from app.notifications.alerts import dispatch_alerts
from app.notifications.telegram import TelegramNotifier
from app.providers.registry import ProviderRegistry
from app.reports.builder import build_historical_report, build_hourly_report
from app.reports.render import render_historical_telegram, render_hourly_telegram
from app.services.collector import FlightCollector

log = get_logger(__name__)

#: One registry per process so HTTP connections and the response cache are reused
#: across cycles - a direct cost saving on metered APIs.
_registry = ProviderRegistry()
_collector = FlightCollector(_registry)
_notifier = TelegramNotifier()


async def collect_job() -> dict[str, Any]:
    """Poll the provider chain and persist a new observation snapshot."""
    try:
        with session_scope() as session:
            summary = await _collector.run_once(session, trigger="scheduler")

        # Alerts run in their own transaction so a delivery problem cannot roll
        # back the observations we just collected.
        if summary.success:
            with session_scope() as session:
                flights = _recent_flights(session)
                tally = await dispatch_alerts(session, flights, _notifier)
            return {**summary.as_dict(), "alerts": tally}
        return summary.as_dict()
    except Exception as exc:  # keep the scheduler alive for the next cycle
        log.exception("job.collect_failed", error=str(exc))
        return {"success": False, "error": str(exc)}


async def aggregate_job() -> dict[str, Any]:
    """Refresh materialised daily/hourly statistics."""
    try:
        with session_scope() as session:
            results = aggregate_recent(session, days=2)
        return {"success": True, "days": results}
    except Exception as exc:
        log.exception("job.aggregate_failed", error=str(exc))
        return {"success": False, "error": str(exc)}


async def hourly_report_job() -> dict[str, Any]:
    """Build the hourly report and push it to Telegram."""
    try:
        with session_scope() as session:
            report = build_hourly_report(session)
            text = render_hourly_telegram(report)

            if not settings.telegram_send_hourly_report:
                log.info("job.hourly_report_built", delivery="disabled")
                return {"success": True, "delivered": False, "reason": "disabled"}

            # Keyed to the local hour so a restart inside the same hour does not
            # re-send, but the next hour always does.
            local = report.generated_at_local[:13]  # "YYYY-MM-DD HH"
            result = await _notifier.send(
                session,
                alert_type=AlertType.HOURLY_REPORT,
                dedup_key=f"HOURLY_REPORT:{local}",
                text=text,
            )
        log.info("job.hourly_report", delivered=result.sent, reason=result.skipped_reason)
        return {
            "success": True,
            "delivered": result.sent,
            "reason": result.skipped_reason or result.error,
        }
    except Exception as exc:
        log.exception("job.hourly_report_failed", error=str(exc))
        return {"success": False, "error": str(exc)}


async def daily_report_job() -> dict[str, Any]:
    """Build and deliver the rolling historical summary once per local day."""
    try:
        with session_scope() as session:
            report = build_historical_report(session)
            text = render_historical_telegram(report)
            result = await _notifier.send(
                session,
                alert_type=AlertType.DAILY_REPORT,
                dedup_key=f"DAILY_REPORT:{local_today().isoformat()}",
                text=text,
            )
        log.info("job.daily_report", delivered=result.sent, reason=result.skipped_reason)
        return {
            "success": True,
            "delivered": result.sent,
            "reason": result.skipped_reason or result.error,
        }
    except Exception as exc:
        log.exception("job.daily_report_failed", error=str(exc))
        return {"success": False, "error": str(exc)}


async def shutdown() -> None:
    """Release provider HTTP connections on process exit."""
    await _registry.aclose()


def _recent_flights(session: Session) -> list[Flight]:
    """Flights worth alerting on: today's board plus the next 24 hours."""
    today_start, _ = local_day_bounds(local_today())
    horizon = utcnow() + timedelta(hours=24)
    return list(
        session.scalars(
            select(Flight).where(
                Flight.is_mock.is_(False),
                Flight.scheduled_departure_utc >= today_start,
                Flight.scheduled_departure_utc <= horizon,
            )
        ).all()
    )
