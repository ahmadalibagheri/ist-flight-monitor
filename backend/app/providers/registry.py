"""Provider registry and failover.

The collector asks for *flights*, not for a particular vendor. This module owns
the mapping from configuration to live provider objects and the decision of which
one to actually call.

Failover walks ``PROVIDER_CHAIN`` in order and stops at the first provider that
returns data. A provider is skipped when it is unconfigured, when it has failed
``PROVIDER_FAILURE_THRESHOLD`` times in a row, or while it is in a rate-limit
cooldown. Health is persisted in ``provider_health`` so the state survives
restarts.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.enums import ProviderKind
from app.core.logging_config import get_logger
from app.core.timeutil import utcnow
from app.models import ProviderHealth
from app.providers.aerodatabox import AeroDataBoxProvider
from app.providers.aviationstack import AviationStackProvider
from app.providers.base import (
    FlightDataProvider,
    ProviderError,
    ProviderNotConfigured,
    ProviderRateLimited,
    ProviderResult,
)
from app.providers.flightaware import FlightAwareProvider
from app.providers.mock import MockProvider

log = get_logger(__name__)

#: Everything the system knows how to talk to. Adding a source means adding one
#: entry here plus the class itself - no other module changes.
PROVIDER_FACTORIES: dict[str, Callable[[], FlightDataProvider]] = {
    "aerodatabox": AeroDataBoxProvider,
    "aviationstack": AviationStackProvider,
    "flightaware": FlightAwareProvider,
    "mock": MockProvider,
}


class ProviderRegistry:
    """Lazily instantiates providers and runs the failover chain."""

    def __init__(self, chain: list[str] | None = None) -> None:
        self._chain = [name.strip().lower() for name in (chain or settings.provider_chain)]
        self._instances: dict[str, FlightDataProvider] = {}

    # ------------------------------------------------------------- lifecycle
    def get(self, name: str) -> FlightDataProvider:
        key = name.strip().lower()
        if key not in PROVIDER_FACTORIES:
            raise ProviderNotConfigured(
                f"Unknown provider '{name}'. Known providers: "
                f"{', '.join(sorted(PROVIDER_FACTORIES))}",
                provider=key,
            )
        if key not in self._instances:
            self._instances[key] = PROVIDER_FACTORIES[key]()
        return self._instances[key]

    @property
    def chain(self) -> list[str]:
        return list(self._chain)

    def describe(self) -> list[dict[str, object]]:
        """Provider inventory for ``GET /providers`` and the readiness probe."""
        out: list[dict[str, object]] = []
        for name in PROVIDER_FACTORIES:
            provider = self.get(name)
            out.append(
                {
                    "name": provider.name,
                    "kind": provider.kind.value,
                    "description": provider.description,
                    "configured": provider.is_configured,
                    "in_chain": name in self._chain,
                    "chain_position": self._chain.index(name) if name in self._chain else None,
                }
            )
        return out

    async def aclose(self) -> None:
        for provider in self._instances.values():
            await provider.aclose()
        self._instances.clear()

    # -------------------------------------------------------------- failover
    async def fetch_with_failover(
        self,
        session: Session,
        origin: str,
        destinations: list[str],
        window_start: datetime,
        window_end: datetime,
    ) -> tuple[ProviderResult | None, list[str], list[str]]:
        """Try each provider in order until one succeeds.

        Returns:
            ``(result, attempted, errors)``. ``result`` is ``None`` when every
            provider in the chain failed - the caller records a failed run rather
            than writing a partial or empty board, because "no flights returned"
            and "provider down" must not look the same in the history.
        """
        attempted: list[str] = []
        errors: list[str] = []
        now = utcnow()

        for name in self._chain:
            try:
                provider = self.get(name)
            except ProviderNotConfigured as exc:
                errors.append(str(exc))
                continue

            if not provider.is_configured:
                log.debug("provider.skip_unconfigured", provider=name)
                errors.append(f"{name}: not configured")
                continue

            if provider.kind is ProviderKind.MOCK and settings.environment == "production":
                errors.append(f"{name}: mock provider blocked in production")
                continue

            health = _load_health(session, name)
            if health.cooldown_until and health.cooldown_until > now:
                log.warning(
                    "provider.in_cooldown",
                    provider=name,
                    until=health.cooldown_until.isoformat(),
                )
                errors.append(f"{name}: in cooldown until {health.cooldown_until.isoformat()}")
                continue
            if not health.is_healthy and health.consecutive_failures >= settings.provider_failure_threshold:
                recovered = (
                    health.last_failure_at is not None
                    and now - health.last_failure_at
                    > timedelta(seconds=settings.provider_recovery_seconds)
                )
                if not recovered:
                    errors.append(f"{name}: unhealthy ({health.consecutive_failures} failures)")
                    continue
                log.info("provider.recovery_attempt", provider=name)

            attempted.append(name)
            started = utcnow()
            try:
                result = await provider.fetch_departures(
                    origin, destinations, window_start, window_end
                )
            except ProviderRateLimited as exc:
                cooldown = exc.retry_after_seconds or settings.provider_recovery_seconds
                _record_failure(session, health, str(exc), cooldown_seconds=cooldown)
                errors.append(f"{name}: rate limited")
                log.warning("provider.rate_limited", provider=name, cooldown_s=cooldown)
                continue
            except ProviderError as exc:
                _record_failure(session, health, str(exc))
                errors.append(f"{name}: {exc}")
                log.warning("provider.failed", provider=name, error=str(exc))
                continue
            except Exception as exc:  # defensive: a parser bug must not kill the run
                _record_failure(session, health, f"unexpected: {exc}")
                errors.append(f"{name}: unexpected error {exc}")
                log.exception("provider.unexpected_error", provider=name)
                continue

            latency_ms = (utcnow() - started).total_seconds() * 1000
            result.latency_ms = latency_ms
            _record_success(session, health, latency_ms)
            log.info(
                "provider.selected",
                provider=name,
                flights=len(result.flights),
                latency_ms=round(latency_ms, 1),
            )
            return result, attempted, errors

        log.error("provider.all_failed", attempted=attempted, errors=errors)
        return None, attempted, errors


    async def fetch_all(
        self,
        session: Session,
        origin: str,
        destinations: list[str],
        window_start: datetime,
        window_end: datetime,
    ) -> tuple[list[ProviderResult], list[str], list[str]]:
        """Query **every** usable provider, for merge mode.

        Unlike :meth:`fetch_with_failover` this does not stop at the first success:
        enrichment needs all of them. A provider that fails is skipped and recorded,
        but does not abort the cycle - partial enrichment beats none.
        """
        results: list[ProviderResult] = []
        attempted: list[str] = []
        errors: list[str] = []
        now = utcnow()

        for name in self._chain:
            try:
                provider = self.get(name)
            except ProviderNotConfigured as exc:
                errors.append(str(exc))
                continue

            if not provider.is_configured:
                errors.append(f"{name}: not configured")
                continue
            if provider.kind is ProviderKind.MOCK and settings.environment == "production":
                errors.append(f"{name}: mock provider blocked in production")
                continue

            health = _load_health(session, name)
            if health.cooldown_until and health.cooldown_until > now:
                errors.append(f"{name}: in cooldown")
                continue

            attempted.append(name)
            started = utcnow()
            try:
                result = await provider.fetch_departures(
                    origin, destinations, window_start, window_end
                )
            except ProviderRateLimited as exc:
                cooldown = exc.retry_after_seconds or settings.provider_recovery_seconds
                _record_failure(session, health, str(exc), cooldown_seconds=cooldown)
                errors.append(f"{name}: rate limited")
                continue
            except ProviderError as exc:
                _record_failure(session, health, str(exc))
                errors.append(f"{name}: {exc}")
                continue
            except Exception as exc:  # a parser bug in one source must not kill the rest
                _record_failure(session, health, f"unexpected: {exc}")
                errors.append(f"{name}: unexpected error {exc}")
                log.exception("provider.unexpected_error", provider=name)
                continue

            latency_ms = (utcnow() - started).total_seconds() * 1000
            result.latency_ms = latency_ms
            _record_success(session, health, latency_ms)
            results.append(result)
            log.info(
                "provider.contributed",
                provider=name,
                flights=len(result.flights),
                latency_ms=round(latency_ms, 1),
            )

        if not results:
            log.error("provider.all_failed", attempted=attempted, errors=errors)
        return results, attempted, errors


# ------------------------------------------------------------------- health IO
def _load_health(session: Session, name: str) -> ProviderHealth:
    health = session.scalar(select(ProviderHealth).where(ProviderHealth.provider == name))
    if health is None:
        health = ProviderHealth(provider=name, is_healthy=True, consecutive_failures=0)
        session.add(health)
        session.flush()
    return health


def _record_success(session: Session, health: ProviderHealth, latency_ms: float) -> None:
    health.is_healthy = True
    health.consecutive_failures = 0
    health.total_calls += 1
    health.last_success_at = utcnow()
    health.last_latency_ms = latency_ms
    health.last_error = None
    health.cooldown_until = None
    session.flush()


def _record_failure(
    session: Session,
    health: ProviderHealth,
    error: str,
    cooldown_seconds: float | None = None,
) -> None:
    health.total_calls += 1
    health.total_failures += 1
    health.consecutive_failures += 1
    health.last_failure_at = utcnow()
    health.last_error = error[:1000]
    health.is_healthy = health.consecutive_failures < settings.provider_failure_threshold
    if cooldown_seconds:
        health.cooldown_until = utcnow() + timedelta(seconds=cooldown_seconds)
    session.flush()
