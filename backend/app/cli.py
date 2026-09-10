"""Command-line entry points for one-shot operational tasks.

Exists so that cron (or systemd, or a Kubernetes CronJob) can drive the pipeline
without the API being reachable, and without exposing the unauthenticated
``/admin`` endpoints to whatever is running the schedule.

    python -m app.cli collect
    python -m app.cli aggregate --days 30
    python -m app.cli report --send
    python -m app.cli status

Every command exits non-zero on failure so cron's own error mail - or a wrapper
script's ``||`` branch - actually fires.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import date, datetime, timedelta
from typing import Any

from sqlalchemy import select

from app.analytics.aggregator import aggregate_recent, rebuild_range
from app.analytics.queries import local_today
from app.core.config import settings
from app.core.db import session_scope
from app.core.enums import AlertType
from app.core.logging_config import configure_logging, get_logger
from app.core.timeutil import utcnow
from app.models import CollectionRun, Flight, ProviderHealth
from app.notifications.alerts import dispatch_alerts
from app.notifications.telegram import TelegramNotifier
from app.providers.registry import ProviderRegistry
from app.reports.builder import build_historical_report, build_hourly_report
from app.reports.render import (
    render_historical_telegram,
    render_historical_text,
    render_hourly_telegram,
    render_hourly_text,
)
from app.scheduler.jobs import _recent_flights
from app.services.collector import FlightCollector

log = get_logger("cli")

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_MISCONFIGURED = 2


# --------------------------------------------------------------------- collect
async def _collect(send_alerts: bool) -> int:
    registry = ProviderRegistry()

    configured = [
        p for p in registry.describe() if p["in_chain"] and p["configured"]
    ]
    if not configured:
        log.error(
            "cli.no_provider",
            chain=registry.chain,
            hint="Set an API key and add the provider to PROVIDER_CHAIN; see docs/PROVIDERS.md",
        )
        return EXIT_MISCONFIGURED

    collector = FlightCollector(registry)
    try:
        with session_scope() as session:
            summary = await collector.run_once(session, trigger="cron")

        if not summary.success:
            log.error("cli.collect_failed", errors=summary.errors)
            return EXIT_FAILED

        payload: dict[str, Any] = summary.as_dict()

        if send_alerts:
            with session_scope() as session:
                tally = await dispatch_alerts(session, _recent_flights(session), TelegramNotifier())
            payload["alerts"] = tally

        # Aggregation is cheap and keeps the dashboard's cached statistics in step
        # with the observations we just wrote.
        with session_scope() as session:
            payload["aggregated"] = aggregate_recent(session, days=2)

        print(json.dumps(payload, indent=2, default=str))
        return EXIT_OK
    finally:
        await registry.aclose()


# ------------------------------------------------------------------- aggregate
def _aggregate(days: int, start: date | None, end: date | None) -> int:
    finish = end or local_today()
    begin = start or finish - timedelta(days=days - 1)
    if begin > finish:
        log.error("cli.bad_range", start=str(begin), end=str(finish))
        return EXIT_MISCONFIGURED
    with session_scope() as session:
        results = rebuild_range(session, begin, finish)
    print(json.dumps({"start": str(begin), "end": str(finish), "days": results}, indent=2))
    return EXIT_OK


# ---------------------------------------------------------------------- report
async def _report(kind: str, send: bool, days: int) -> int:
    notifier = TelegramNotifier()
    with session_scope() as session:
        # Kept as separate branches rather than one reused variable so each report
        # type stays concretely typed all the way to its renderer.
        if kind == "daily":
            historical = build_historical_report(session, days=days)
            text = render_historical_text(historical)
            message = render_historical_telegram(historical)
            alert_type = AlertType.DAILY_REPORT
            key = f"DAILY_REPORT:{local_today().isoformat()}"
        else:
            hourly = build_hourly_report(session)
            text = render_hourly_text(hourly)
            message = render_hourly_telegram(hourly)
            alert_type = AlertType.HOURLY_REPORT
            key = f"HOURLY_REPORT:{hourly.generated_at_local[:13]}"

        print(text)

        if not send:
            return EXIT_OK
        if not notifier.is_configured:
            log.warning("cli.telegram_not_configured")
            return EXIT_OK
        result = await notifier.send(session, alert_type=alert_type, dedup_key=key, text=message)

    if result.error:
        log.error("cli.report_delivery_failed", error=result.error)
        return EXIT_FAILED
    log.info("cli.report", delivered=result.sent, reason=result.skipped_reason)
    return EXIT_OK


# ---------------------------------------------------------------------- status
def _status() -> int:
    """Operational snapshot, suitable for a cron health check or a quick look."""
    registry = ProviderRegistry()
    with session_scope() as session:
        last = session.scalar(
            select(CollectionRun).order_by(CollectionRun.started_at.desc()).limit(1)
        )
        health = {
            row.provider: {
                "healthy": row.is_healthy,
                "consecutive_failures": row.consecutive_failures,
                "last_success_at": row.last_success_at.isoformat() if row.last_success_at else None,
                "last_error": row.last_error,
            }
            for row in session.scalars(select(ProviderHealth)).all()
        }
        real_flights = session.scalar(
            select(Flight).where(Flight.is_mock.is_(False)).limit(1)
        )
        counts = {
            "flights": session.query(Flight).count(),
            "real_flights": session.query(Flight).filter(Flight.is_mock.is_(False)).count(),
        }

    stale = True
    if last and last.started_at:
        age = (utcnow() - last.started_at).total_seconds() / 60
        # Two missed cycles is the point at which something is actually wrong.
        stale = age > settings.collection_interval_minutes * 2

    payload = {
        "provider_chain": registry.chain,
        "collection_interval_minutes": settings.collection_interval_minutes,
        "last_run": {
            "id": last.id if last else None,
            "started_at": last.started_at.isoformat() if last else None,
            "provider": last.provider if last else None,
            "success": last.success if last else None,
            "flights_seen": last.flights_seen if last else None,
        },
        "last_run_is_stale": stale,
        "provider_health": health,
        "counts": counts,
        "has_real_data": real_flights is not None,
    }
    print(json.dumps(payload, indent=2, default=str))
    # Non-zero when collection has stopped, so cron can alert on it.
    return EXIT_FAILED if (stale or (last and not last.success)) else EXIT_OK


# ------------------------------------------------------------------------ main
def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m app.cli",
        description="One-shot operational commands for the IST Flight Reliability Monitor.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    collect = sub.add_parser("collect", help="Run one collection cycle and aggregate.")
    collect.add_argument(
        "--no-alerts",
        action="store_true",
        help="Collect without dispatching Telegram alerts.",
    )

    aggregate = sub.add_parser("aggregate", help="Rebuild materialised statistics.")
    aggregate.add_argument("--days", type=int, default=30)
    aggregate.add_argument("--start", type=_parse_date, default=None)
    aggregate.add_argument("--end", type=_parse_date, default=None)

    report = sub.add_parser("report", help="Print (and optionally send) a report.")
    report.add_argument("--kind", choices=["hourly", "daily"], default="hourly")
    report.add_argument("--send", action="store_true", help="Deliver to Telegram.")
    report.add_argument("--days", type=int, default=30, help="Window for the daily report.")

    sub.add_parser("status", help="Operational snapshot; exits non-zero if collection has stalled.")
    return parser


def _parse_date(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


def main(argv: list[str] | None = None) -> int:
    configure_logging()
    args = build_parser().parse_args(argv)

    if args.command == "collect":
        return asyncio.run(_collect(send_alerts=not args.no_alerts))
    if args.command == "aggregate":
        return _aggregate(args.days, args.start, args.end)
    if args.command == "report":
        return asyncio.run(_report(args.kind, args.send, args.days))
    if args.command == "status":
        return _status()
    return EXIT_MISCONFIGURED


if __name__ == "__main__":
    sys.exit(main())
