"""Liveness, readiness and operational introspection."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter
from sqlalchemy import func, select, text

from app.api.deps import DbSession
from app.core.config import settings
from app.core.logging_config import get_logger
from app.core.timeutil import to_local, utcnow
from app.models import CollectionRun, Flight, ProviderHealth
from app.notifications.telegram import TelegramNotifier
from app.providers.registry import ProviderRegistry
from app.schemas.common import HealthOut, ProviderOut, ReadinessOut

log = get_logger(__name__)
router = APIRouter(tags=["health"])

VERSION = "1.0.0"


@router.get("/health", response_model=HealthOut, summary="Liveness probe")
def health() -> HealthOut:
    """Cheap liveness check. Touches nothing external."""
    now = utcnow()
    local = to_local(now)
    return HealthOut(
        status="ok",
        version=VERSION,
        environment=settings.environment,
        timezone=settings.operational_timezone,
        time_utc=now,
        time_local=local.strftime("%Y-%m-%d %H:%M:%S %Z") if local else "",
    )


@router.get("/ready", response_model=ReadinessOut, summary="Readiness probe")
async def ready(session: DbSession) -> ReadinessOut:
    """Deep check: database reachable, a real provider configured, Telegram sane.

    Readiness deliberately fails when no REAL provider is configured - the service
    would start happily but could never collect anything, and a green probe would
    hide that.
    """
    checks: dict[str, Any] = {}
    ok = True

    try:
        session.execute(text("SELECT 1"))
        checks["database"] = {"ok": True}
    except Exception as exc:
        ok = False
        checks["database"] = {"ok": False, "error": str(exc)}

    registry = ProviderRegistry()
    described = registry.describe()
    real_configured = [
        p for p in described if p["kind"] == "REAL" and p["configured"] and p["in_chain"]
    ]
    checks["providers"] = {
        "ok": bool(real_configured),
        "configured_real_providers": [p["name"] for p in real_configured],
        "chain": registry.chain,
        "detail": (
            None
            if real_configured
            else "No REAL provider in PROVIDER_CHAIN is configured; see docs/PROVIDERS.md"
        ),
    }
    if not real_configured:
        ok = False

    notifier = TelegramNotifier()
    checks["telegram"] = {
        "ok": True,  # Telegram is optional; never blocks readiness.
        "configured": notifier.is_configured,
    }

    last_run = session.scalar(
        select(CollectionRun).order_by(CollectionRun.started_at.desc()).limit(1)
    )
    checks["last_collection"] = (
        {
            "ok": last_run.success,
            "at": last_run.started_at.isoformat(),
            "provider": last_run.provider,
            "flights_seen": last_run.flights_seen,
        }
        if last_run
        else {"ok": False, "detail": "no collection run has completed yet"}
    )

    checks["real_flights_stored"] = session.scalar(
        select(func.count()).select_from(Flight).where(Flight.is_mock.is_(False))
    )
    return ReadinessOut(ready=ok, checks=checks)


@router.get("/providers", response_model=list[ProviderOut], summary="Provider inventory")
def providers(session: DbSession) -> list[ProviderOut]:
    """Every known provider, whether it is configured, and its recorded health."""
    registry = ProviderRegistry()
    health_rows = {
        row.provider: row for row in session.scalars(select(ProviderHealth)).all()
    }
    out: list[ProviderOut] = []
    for described in registry.describe():
        row = health_rows.get(str(described["name"]))
        out.append(
            ProviderOut(
                name=str(described["name"]),
                kind=str(described["kind"]),
                description=str(described["description"]),
                configured=bool(described["configured"]),
                in_chain=bool(described["in_chain"]),
                chain_position=described["chain_position"],  # type: ignore[arg-type]
                is_healthy=row.is_healthy if row else None,
                consecutive_failures=row.consecutive_failures if row else None,
                last_success_at=row.last_success_at if row else None,
                last_failure_at=row.last_failure_at if row else None,
                last_error=row.last_error if row else None,
            )
        )
    return out
