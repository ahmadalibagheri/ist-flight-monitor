"""Operational endpoints: trigger a collection, rebuild statistics, test Telegram.

These mutate state, so they are POST-only and are intended to sit behind whatever
authentication the deployment already terminates at (reverse proxy, VPN, or an
ingress rule). They exist because "wait an hour and see" is a poor debugging loop.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Annotated

from fastapi import APIRouter, HTTPException, Query, status

from app.analytics.aggregator import rebuild_range
from app.analytics.queries import local_today
from app.api.deps import DbSession
from app.core.config import settings
from app.core.enums import AlertType
from app.core.logging_config import get_logger
from app.core.timeutil import utcnow
from app.notifications.telegram import TelegramNotifier
from app.providers.registry import ProviderRegistry
from app.services.collector import FlightCollector

log = get_logger(__name__)
router = APIRouter(prefix="/admin", tags=["admin"])


@router.post("/collect", summary="Run one collection cycle now")
async def trigger_collection(session: DbSession) -> dict[str, object]:
    """Runs synchronously and returns the same summary the scheduler logs."""
    registry = ProviderRegistry()
    collector = FlightCollector(registry)
    try:
        summary = await collector.run_once(session, trigger="manual")
        session.commit()
    finally:
        await registry.aclose()

    if not summary.success:
        # 503: the service is fine, the upstream data source is not.
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={
                "message": "Collection failed - no provider returned data.",
                "errors": summary.errors,
                "hint": "Check GET /providers and docs/PROVIDERS.md",
            },
        )
    return summary.as_dict()


@router.post("/aggregate", summary="Rebuild materialised statistics")
def trigger_aggregation(
    session: DbSession,
    start_date: Annotated[date | None, Query()] = None,
    end_date: Annotated[date | None, Query()] = None,
) -> dict[str, object]:
    """Recompute daily/hourly statistics for a local date range.

    Safe to run at any time: the statistics tables are a cache over the
    observation history and are rebuilt idempotently.
    """
    end = end_date or local_today()
    start = start_date or end - timedelta(days=settings.historical_report_days - 1)
    if start > end:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, detail="start_date must not be after end_date"
        )
    results = rebuild_range(session, start, end)
    session.commit()
    return {"start": start.isoformat(), "end": end.isoformat(), "days": results}


@router.post("/telegram/test", summary="Send a Telegram test message")
async def telegram_test(session: DbSession) -> dict[str, object]:
    """Verifies the bot token and chat id end to end."""
    notifier = TelegramNotifier()
    verification = await notifier.verify()
    if not notifier.is_configured:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            detail={
                "message": "Telegram is not configured.",
                "verification": verification,
                "hint": "Set TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID; see README.",
            },
        )

    stamp = utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
    result = await notifier.send(
        session,
        alert_type=AlertType.HOURLY_REPORT,
        dedup_key=f"TEST:{stamp}",
        text="```\nIST Flight Monitor - test message\nDelivery is working.\n```",
    )
    session.commit()
    return {
        "verification": verification,
        "sent": result.sent,
        "error": result.error,
        "skipped_reason": result.skipped_reason,
    }
