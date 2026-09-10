"""Alert rules: what is worth interrupting someone for, and how it is worded.

Rules implemented (spec section 16):

* a flight newly becoming cancelled
* a delay crossing each configured threshold (60 and 120 minutes by default)
* a route-wide disruption - a share of a route's flights cancelled on one day

Each rule builds a ``dedup_key`` that encodes the *fact*, not the moment. A delay
of 75 minutes and a later delay of 95 minutes share the ``>60`` key, so the
threshold alert fires once; crossing 120 produces a new key and a new alert.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.enums import AlertType, FlightStatus
from app.core.logging_config import get_logger
from app.core.timeutil import to_local
from app.models import Flight
from app.notifications.telegram import TelegramNotifier
from app.reports.render import escape_markdown_v2

log = get_logger(__name__)


@dataclass(slots=True)
class Alert:
    alert_type: AlertType
    dedup_key: str
    text: str
    flight_id: int | None = None


def build_alerts(flights: list[Flight]) -> list[Alert]:
    """Derive every alert warranted by the current state of these flights."""
    alerts: list[Alert] = []

    if settings.telegram_alert_on_cancellation:
        alerts.extend(_cancellation_alerts(flights))
    alerts.extend(_delay_alerts(flights))
    alerts.extend(_route_disruption_alerts(flights))
    return alerts


def _cancellation_alerts(flights: list[Flight]) -> list[Alert]:
    out: list[Alert] = []
    for flight in flights:
        if not flight.is_cancelled:
            continue
        local = to_local(flight.scheduled_departure_utc)
        when = local.strftime("%Y-%m-%d %H:%M") if local else "unknown time"
        reason = f"\nReason: {flight.cancellation_reason}" if flight.cancellation_reason else ""
        body = (
            f"CANCELLED\n\n"
            f"Flight:    {flight.flight_number}\n"
            f"Airline:   {flight.airline_name or flight.airline_iata or 'Unknown'}\n"
            f"Route:     {flight.origin_iata} -> {flight.destination_iata}\n"
            f"Scheduled: {when} ({settings.operational_timezone}){reason}"
        )
        out.append(
            Alert(
                alert_type=AlertType.CANCELLATION,
                dedup_key=f"CANCELLATION:{flight.flight_number}:{flight.flight_date_local}",
                text=_wrap(body),
                flight_id=flight.id,
            )
        )
    return out


def _delay_alerts(flights: list[Flight]) -> list[Alert]:
    out: list[Alert] = []
    thresholds = sorted(settings.telegram_alert_delay_minutes)
    for flight in flights:
        delay = flight.delay_minutes
        if delay is None or flight.is_cancelled:
            continue
        if flight.status in {FlightStatus.ARRIVED}:
            continue
        for threshold in thresholds:
            if delay < threshold:
                continue
            local = to_local(flight.scheduled_departure_utc)
            when = local.strftime("%Y-%m-%d %H:%M") if local else "unknown time"
            body = (
                f"DELAY OVER {threshold} MINUTES\n\n"
                f"Flight:    {flight.flight_number}\n"
                f"Airline:   {flight.airline_name or flight.airline_iata or 'Unknown'}\n"
                f"Route:     {flight.origin_iata} -> {flight.destination_iata}\n"
                f"Scheduled: {when} ({settings.operational_timezone})\n"
                f"Delay:     {delay} minutes\n"
                f"Status:    {flight.status.value}"
            )
            out.append(
                Alert(
                    alert_type=AlertType.DELAY_THRESHOLD,
                    dedup_key=(
                        f"DELAY:{flight.flight_number}:{flight.flight_date_local}:{threshold}"
                    ),
                    text=_wrap(body),
                    flight_id=flight.id,
                )
            )
    return out


def _route_disruption_alerts(flights: list[Flight]) -> list[Alert]:
    """Fire when a whole route is having a bad day, not just one flight."""
    by_route: dict[tuple[str, object], list[Flight]] = defaultdict(list)
    for flight in flights:
        by_route[(f"{flight.origin_iata}-{flight.destination_iata}", flight.flight_date_local)].append(
            flight
        )

    out: list[Alert] = []
    for (route, day), group in by_route.items():
        total = len(group)
        if total < settings.telegram_route_disruption_min_flights:
            continue
        cancelled = sum(1 for f in group if f.is_cancelled)
        rate = cancelled / total
        if rate < settings.telegram_route_disruption_rate:
            continue
        body = (
            f"ROUTE DISRUPTION\n\n"
            f"Route:        {route.replace('-', ' -> ')}\n"
            f"Date:         {day}\n"
            f"Cancelled:    {cancelled} of {total} flights\n"
            f"Cancellation: {rate * 100:.1f}%\n\n"
            f"Threshold is {settings.telegram_route_disruption_rate * 100:.0f}%."
        )
        out.append(
            Alert(
                alert_type=AlertType.ROUTE_DISRUPTION,
                # Bucketed to 10% so a worsening day can re-alert once, but a
                # flight-by-flight drift cannot spam.
                dedup_key=f"DISRUPTION:{route}:{day}:{int(rate * 10)}",
                text=_wrap(body),
            )
        )
    return out


async def dispatch_alerts(
    session: Session, flights: list[Flight], notifier: TelegramNotifier | None = None
) -> dict[str, int]:
    """Build and deliver alerts, returning a per-outcome tally."""
    telegram = notifier or TelegramNotifier()
    alerts = build_alerts(flights)
    tally = {"built": len(alerts), "sent": 0, "duplicate": 0, "failed": 0, "skipped": 0}

    for alert in alerts:
        result = await telegram.send(
            session,
            alert_type=alert.alert_type,
            dedup_key=alert.dedup_key,
            text=alert.text,
            flight_id=alert.flight_id,
        )
        if result.sent:
            tally["sent"] += 1
        elif result.skipped_reason == "duplicate":
            tally["duplicate"] += 1
        elif result.error:
            tally["failed"] += 1
        else:
            tally["skipped"] += 1

    if tally["built"]:
        log.info("alerts.dispatched", **tally)
    return tally


def _wrap(body: str) -> str:
    """Fixed-width body inside a MarkdownV2 code fence."""
    safe = body.replace("\\", "\\\\").replace("`", "\\`")
    return f"```\n{safe}\n```"


__all__ = ["Alert", "build_alerts", "dispatch_alerts", "escape_markdown_v2"]
