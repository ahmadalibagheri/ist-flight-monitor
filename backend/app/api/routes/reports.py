"""Report endpoints. Every report is available as JSON and as rendered text."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Query, Response
from sqlalchemy import select

from app.api.deps import DbSession
from app.core.config import settings
from app.models import CollectionRun
from app.reports.builder import build_historical_report, build_hourly_report
from app.reports.render import render_historical_text, render_hourly_text
from app.schemas.statistics import CollectionRunOut, HistoricalReportOut, HourlyReportOut

router = APIRouter(prefix="/reports", tags=["reports"])


@router.get(
    "/latest",
    response_model=HourlyReportOut,
    summary="The current hourly report",
)
def latest_report(
    session: DbSession,
    history_days: Annotated[int | None, Query(ge=1, le=365)] = None,
    include_text: Annotated[bool, Query()] = True,
) -> HourlyReportOut:
    """Built on demand so it always reflects the newest observation."""
    report = build_hourly_report(session, history_days=history_days)
    payload = report.as_dict()
    payload["text"] = render_hourly_text(report) if include_text else None
    return HourlyReportOut(**payload)  # type: ignore[arg-type]


@router.get(
    "/latest.txt",
    response_class=Response,
    summary="The current hourly report as plain text",
)
def latest_report_text(session: DbSession) -> Response:
    """The exact fixed-width layout that is pushed to Telegram."""
    text = render_hourly_text(build_hourly_report(session))
    return Response(content=text, media_type="text/plain; charset=utf-8")


@router.get(
    "/daily",
    response_model=HistoricalReportOut,
    summary="Rolling historical summary",
)
def daily_report(
    session: DbSession,
    days: Annotated[int, Query(ge=1, le=365)] = 30,
    include_text: Annotated[bool, Query()] = True,
) -> HistoricalReportOut:
    """Defaults to the rolling 30-day window described in spec section 15."""
    report = build_historical_report(session, days=days)
    payload = report.as_dict()
    payload["text"] = render_historical_text(report) if include_text else None
    return HistoricalReportOut(**payload)  # type: ignore[arg-type]


@router.get(
    "/monthly",
    response_model=HistoricalReportOut,
    summary="Rolling 30-day summary",
)
def monthly_report(
    session: DbSession, include_text: Annotated[bool, Query()] = True
) -> HistoricalReportOut:
    """Convenience alias for a 30-day historical report."""
    return daily_report(session, days=30, include_text=include_text)


@router.get(
    "/daily.txt", response_class=Response, summary="Historical summary as plain text"
)
def daily_report_text(
    session: DbSession, days: Annotated[int, Query(ge=1, le=365)] = 30
) -> Response:
    text = render_historical_text(build_historical_report(session, days=days))
    return Response(content=text, media_type="text/plain; charset=utf-8")


@router.get(
    "/collection-runs",
    response_model=list[CollectionRunOut],
    summary="Recent collection cycles",
)
def collection_runs(
    session: DbSession, limit: Annotated[int, Query(ge=1, le=200)] = 25
) -> list[CollectionRunOut]:
    """Operational history: what ran, which provider answered, what it wrote."""
    rows = session.scalars(
        select(CollectionRun).order_by(CollectionRun.started_at.desc()).limit(limit)
    ).all()
    return [CollectionRunOut.model_validate(r) for r in rows]


@router.get("/config", summary="Effective non-secret configuration")
def effective_config() -> dict[str, object]:
    """What the running instance actually believes.

    Only non-secret values are exposed - credentials are reported as a boolean
    "configured" flag elsewhere, never echoed.
    """
    return {
        "environment": settings.environment,
        "timezone": settings.operational_timezone,
        "origin_airport": settings.origin_airport,
        "destination_airports": settings.destination_airports,
        "include_codeshare": settings.include_codeshare,
        "collection_interval_minutes": settings.collection_interval_minutes,
        "collection_lookahead_hours": settings.collection_lookahead_hours,
        "collection_lookback_hours": settings.collection_lookback_hours,
        "stale_after_minutes": settings.stale_after_minutes,
        "provider_chain": settings.provider_chain,
        "delay_threshold_minutes": settings.delay_threshold_minutes,
        "delay_buckets_minutes": settings.delay_buckets_minutes,
        "min_sample_size": {
            "airline": settings.min_sample_size_airline,
            "flight": settings.min_sample_size_flight,
            "period": settings.min_sample_size_period,
        },
        "reliability_score": {
            "weights": {
                "on_time": settings.score_weight_on_time,
                "cancellation": settings.score_weight_cancellation,
                "avg_delay": settings.score_weight_avg_delay,
                "severe_delay": settings.score_weight_severe_delay,
            },
            "anchors": {
                "max_cancellation_rate": settings.score_max_cancellation_rate,
                "max_avg_delay_minutes": settings.score_max_avg_delay_minutes,
                "max_severe_delay_rate": settings.score_max_severe_delay_rate,
            },
        },
    }
