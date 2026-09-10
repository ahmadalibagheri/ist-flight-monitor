"""FastAPI application.

Run with::

    uvicorn app.main:app --reload

The scheduler lives in a separate process (``python -m app.scheduler.runner``) so
that scaling the API horizontally cannot multiply the collection jobs. When
``SCHEDULER_ENABLED=true`` *and* this is the only process, the lifespan hook will
start it in-process as a convenience for local development.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from app.analytics.queries import UnknownRouteFilter
from app.api.routes import admin, flights, health, reports, statistics
from app.core.config import settings
from app.core.logging_config import configure_logging, get_logger
from app.core.timeutil import utcnow
from app.providers.base import ProviderError

log = get_logger(__name__)

DESCRIPTION = """
Monitors direct departures on a configured set of routes and turns the collected
history into delay, cancellation, time-of-day and reliability analytics. Routes are
**directional** — `IST-IKA` and `IKA-IST` are tracked separately and never
collapsed, because an airline's punctuality outbound says little about its return.

Call `GET /api/v1/routes` for what this instance is actually monitoring.

### How to read the numbers

* `cancellation_rate` is over **all** scheduled flights.
* `delay_rate` and `on_time_rate` are over **measurable** flights only - flights
  where the provider supplied enough timing data to compute a delay. Flights
  counted in `unknown_flights` are excluded from both rather than being assumed
  on time.
* A missing metric is reported as `null`, never as `0`.
* Rankings honour a minimum sample size; entries below it are returned with
  `is_ranked: false` and sorted last.
* Synthetic development data (`is_mock: true`) is excluded from every statistic.

### Data sources

Flight data comes from a configurable provider chain - see `GET /providers` for
what this instance is actually using, and `docs/PROVIDERS.md` for setup.
"""


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    configure_logging()
    log.info(
        "app.starting",
        environment=settings.environment,
        timezone=settings.operational_timezone,
        routes=settings.route_labels(),
        provider_chain=settings.provider_chain,
        scheduler_enabled=settings.scheduler_enabled,
    )

    scheduler = None
    if settings.scheduler_enabled:
        # Convenience for `uvicorn app.main:app` during development. In Docker the
        # worker service owns the schedule and the API sets SCHEDULER_ENABLED=false.
        from app.scheduler.runner import build_scheduler

        scheduler = build_scheduler()
        scheduler.start()
        app.state.scheduler = scheduler
        log.info("app.scheduler_started_in_process")
    else:
        app.state.scheduler = None

    try:
        yield
    finally:
        if scheduler is not None:
            scheduler.shutdown(wait=False)
            from app.scheduler.jobs import shutdown as jobs_shutdown

            await jobs_shutdown()
        log.info("app.stopped")


app = FastAPI(
    title=settings.app_name,
    description=DESCRIPTION,
    version="1.0.0",
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "OPTIONS"],
    allow_headers=["*"],
)


@app.exception_handler(ProviderError)
async def provider_error_handler(request: Request, exc: ProviderError) -> JSONResponse:
    """Upstream problems are 503, not 500 - the service itself is healthy."""
    log.warning("api.provider_error", path=request.url.path, error=str(exc))
    return JSONResponse(
        status_code=503,
        content={
            "detail": str(exc),
            "provider": exc.provider,
            "retryable": exc.retryable,
        },
    )


# Health and provider introspection sit at the root so probes need no prefix.
app.include_router(health.router)

for router in (flights.router, statistics.router, reports.router, admin.router):
    app.include_router(router, prefix=settings.api_prefix)


@app.exception_handler(UnknownRouteFilter)
async def unknown_route_handler(request: Request, exc: UnknownRouteFilter) -> JSONResponse:
    """A malformed route filter is the caller's mistake, not a server fault.

    The route query parameters validate before reaching the analytics layer, so this
    only fires for a path that bypassed them - but answering 400 with the monitored
    routes is far more useful than a 500.
    """
    log.info("api.unknown_route_filter", path=request.url.path, detail=str(exc))
    return JSONResponse(
        status_code=400,
        content={"detail": str(exc), "routes": settings.route_labels()},
    )


@app.get("/", include_in_schema=False)
def root() -> dict[str, object]:
    return {
        "name": settings.app_name,
        "version": "1.0.0",
        "time_utc": utcnow().isoformat(),
        "docs": "/docs",
        "health": "/health",
        "readiness": "/ready",
        "api_prefix": settings.api_prefix,
        "routes_monitored": settings.route_labels(),
    }
