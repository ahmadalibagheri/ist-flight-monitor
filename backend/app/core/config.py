"""Application configuration.

Every tunable is an environment variable; nothing operational is hard-coded and no
secret ever has a real default. See ``.env.example`` at the repository root.
"""

from __future__ import annotations

import json
from functools import lru_cache
from typing import Annotated, Any, Literal
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import Field, ValidationInfo, field_validator, model_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

CollectionInterval = Literal[5, 15, 30, 60]


class Settings(BaseSettings):
    """Runtime settings loaded from the environment."""

    model_config = SettingsConfigDict(
        env_file=(".env", "../.env"),
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ------------------------------------------------------------------ app
    app_name: str = "IST Flight Reliability Monitor"
    environment: Literal["development", "staging", "production"] = "development"
    log_level: str = "INFO"
    log_format: Literal["json", "console"] = "json"
    api_prefix: str = "/api/v1"
    cors_origins: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["http://localhost:3000"]
    )

    # ------------------------------------------------------------- database
    database_url: str = "postgresql+psycopg://flightmon:flightmon@localhost:5432/flightmon"
    db_pool_size: int = 5
    db_max_overflow: int = 10
    db_echo: bool = False

    # ------------------------------------------------------------- timezone
    # Operational timezone. Everything is *stored* in UTC; this is the timezone
    # used to bucket observations into local hours/days for analytics and reports.
    operational_timezone: str = "Europe/Istanbul"

    # --------------------------------------------------------------- routes
    #: Explicit directional route pairs, e.g. ``IST-IKA,IST-MHD,IKA-IST``.
    #:
    #: Direction matters: IST-IKA and IKA-IST are separate routes with separate
    #: statistics. Each distinct *origin* costs its own provider call set, because
    #: the underlying APIs serve one airport's departure board per request - see
    #: docs/PROVIDERS.md before adding one.
    #:
    #: Left empty, routes are derived from ``origin_airport`` + ``destination_airports``
    #: so existing configurations keep working.
    monitored_routes: Annotated[list[str], NoDecode] = Field(default_factory=list)

    #: Legacy single-origin form. Still honoured when ``monitored_routes`` is empty.
    origin_airport: str = "IST"
    destination_airports: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["IKA", "MHD"]
    )
    include_codeshare: bool = False
    include_indirect: bool = False

    # ------------------------------------------------------------ collector
    collection_interval_minutes: CollectionInterval = 60
    # How far forward/backward from "now" each collection cycle looks, in hours.
    collection_lookahead_hours: int = 36
    collection_lookback_hours: int = 6
    collection_timeout_seconds: float = 30.0
    collection_max_retries: int = 4
    collection_backoff_base_seconds: float = 1.5
    collection_backoff_max_seconds: float = 60.0
    # A flight not seen in a provider response for this long is *not* assumed
    # cancelled - it is marked stale. Missing data is never a cancellation.
    stale_after_minutes: int = 180
    # Import the bundled observation history when the database is empty, so a fresh
    # checkout can compute analytics instead of showing an empty dashboard. Only ever
    # applies to an empty database, and is ignored when ENVIRONMENT=production.
    seed_on_empty: bool = True

    # Persist each provider response verbatim alongside the parsed observation.
    # Historical responses cannot be re-fetched, so anything not stored is lost
    # permanently; ~1 KB per observation buys full recoverability.
    store_raw_payload: bool = True

    # ------------------------------------------------------------ providers
    # Ordered provider chain. Also the trust order used when merging.
    provider_chain: Annotated[list[str], NoDecode] = Field(
        default_factory=lambda: ["aerodatabox"]
    )
    #: ``failover`` - stop at the first provider that answers (cheapest).
    #: ``merge``    - query every configured provider and combine their views,
    #:               so a source that knows the gate can fill in for one that
    #:               only knows the schedule. Costs one call set per provider.
    provider_strategy: Literal["failover", "merge"] = "failover"
    allow_mock_provider: bool = False

    aerodatabox_api_key: str | None = None
    aerodatabox_base_url: str = "https://aerodatabox.p.rapidapi.com"
    # RapidAPI wants X-RapidAPI-Key + X-RapidAPI-Host; the direct and API.market
    # channels use a different header name, so both are configurable.
    aerodatabox_auth_header: str = "X-RapidAPI-Key"
    aerodatabox_host_header: str | None = "aerodatabox.p.rapidapi.com"
    # AeroDataBox caps one FIDS request at 12 hours; the collector chunks around it.
    aerodatabox_window_hours: int = 12

    aviationstack_api_key: str | None = None
    aviationstack_base_url: str = "https://api.aviationstack.com/v1"

    flightaware_api_key: str | None = None
    flightaware_base_url: str = "https://aeroapi.flightaware.com/aeroapi"

    # Cache TTL for provider responses, keyed by request window (cost control).
    provider_cache_ttl_seconds: int = 240
    # Consecutive failures before a provider is considered unhealthy.
    provider_failure_threshold: int = 3
    provider_recovery_seconds: int = 900

    # ------------------------------------------------------------ analytics
    # A departure is "delayed" once it exceeds this many minutes.
    delay_threshold_minutes: int = 15
    delay_buckets_minutes: Annotated[list[int], NoDecode] = Field(
        default_factory=lambda: [15, 30, 60, 120]
    )
    # Minimum observed flights before an airline/flight is ranked at all.
    min_sample_size_airline: int = 20
    min_sample_size_flight: int = 10
    min_sample_size_period: int = 10

    # ---------------------------------------------- reliability score weights
    # Transparent, configurable 0-100 score. Weights must sum to 1.0.
    score_weight_on_time: float = 0.35
    score_weight_cancellation: float = 0.30
    score_weight_avg_delay: float = 0.20
    score_weight_severe_delay: float = 0.15
    # Normalisation anchors: the value at which a component scores zero.
    score_max_cancellation_rate: float = 0.25
    score_max_avg_delay_minutes: float = 90.0
    score_max_severe_delay_rate: float = 0.35

    # ------------------------------------------------------------- reports
    report_interval_minutes: int = 60
    daily_report_hour_local: int = 23
    historical_report_days: int = 30

    # ------------------------------------------------------------ telegram
    telegram_bot_token: str | None = None
    telegram_chat_id: str | None = None
    telegram_enabled: bool = True
    telegram_send_hourly_report: bool = True
    telegram_alert_delay_minutes: Annotated[list[int], NoDecode] = Field(
        default_factory=lambda: [60, 120]
    )
    telegram_alert_on_cancellation: bool = True
    # A route disruption alert fires when this share of a route's flights in the
    # current local day are cancelled (with at least `_min_flights` flights).
    telegram_route_disruption_rate: float = 0.20
    telegram_route_disruption_min_flights: int = 4
    telegram_timeout_seconds: float = 15.0

    # ------------------------------------------------------------ scheduler
    scheduler_enabled: bool = True
    aggregation_interval_minutes: int = 30

    # -------------------------------------------------------------- helpers
    @field_validator(
        "cors_origins",
        "destination_airports",
        "monitored_routes",
        "provider_chain",
        "delay_buckets_minutes",
        "telegram_alert_delay_minutes",
        mode="before",
    )
    @classmethod
    def _split_list(cls, value: Any, info: ValidationInfo) -> Any:
        """Accept both JSON arrays and plain comma-separated env values.

        These fields are annotated ``NoDecode`` so pydantic-settings hands over the
        raw environment string instead of trying ``json.loads`` on it first - that
        is what lets ``PROVIDER_CHAIN=aerodatabox,flightaware`` work.
        """
        if not isinstance(value, str):
            return value
        text = value.strip()
        if not text:
            return []
        if text.startswith("["):
            return json.loads(text)
        parts = [part.strip() for part in text.split(",") if part.strip()]
        if info.field_name in {"delay_buckets_minutes", "telegram_alert_delay_minutes"}:
            return [int(part) for part in parts]
        return parts

    @field_validator("collection_interval_minutes", mode="before")
    @classmethod
    def _coerce_interval(cls, value: Any) -> Any:
        """Environment variables arrive as strings; a Literal[int] will not coerce.

        Without this, ``COLLECTION_INTERVAL_MINUTES=60`` from a ``.env`` file or a
        compose ``environment:`` block fails validation with a confusing
        ``Input should be 5, 15, 30 or 60 ... input_value='60'``.
        """
        if isinstance(value, str):
            text = value.strip()
            if text.isdigit():
                return int(text)
        return value

    @field_validator("origin_airport")
    @classmethod
    def _upper_origin(cls, value: str) -> str:
        return value.strip().upper()

    @field_validator("destination_airports")
    @classmethod
    def _upper_destinations(cls, value: list[str]) -> list[str]:
        return [item.strip().upper() for item in value]

    @field_validator("monitored_routes")
    @classmethod
    def _valid_routes(cls, value: list[str]) -> list[str]:
        normalised: list[str] = []
        for entry in value:
            origin, destination = _parse_route(entry)
            if origin == destination:
                raise ValueError(
                    f"Route {entry!r} has the same origin and destination."
                )
            pair = f"{origin}-{destination}"
            if pair not in normalised:
                normalised.append(pair)
        return normalised

    @field_validator("operational_timezone")
    @classmethod
    def _valid_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            # ZoneInfoNotFoundError subclasses KeyError, which pydantic would let
            # escape raw. Re-raise as ValueError so a typo in OPERATIONAL_TIMEZONE
            # fails startup with a readable message instead of a KeyError.
            raise ValueError(
                f"Unknown timezone {value!r}. Use an IANA name such as 'Europe/Istanbul'."
            ) from exc
        return value

    @model_validator(mode="after")
    def _check_weights(self) -> Settings:
        total = (
            self.score_weight_on_time
            + self.score_weight_cancellation
            + self.score_weight_avg_delay
            + self.score_weight_severe_delay
        )
        if abs(total - 1.0) > 1e-6:
            raise ValueError(
                f"Reliability score weights must sum to 1.0, got {total:.4f}. "
                "Adjust SCORE_WEIGHT_* environment variables."
            )
        return self

    # ------------------------------------------------------------ accessors
    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.operational_timezone)

    @property
    def routes(self) -> list[tuple[str, str]]:
        """Monitored routes as ``(origin, destination)`` pairs, in configured order.

        Direction is significant throughout the system: ``IST-IKA`` and ``IKA-IST``
        are distinct routes with distinct statistics, and nothing collapses them.
        """
        if self.monitored_routes:
            return [_parse_route(entry) for entry in self.monitored_routes]
        return [(self.origin_airport, dest) for dest in self.destination_airports]

    @property
    def origins(self) -> list[str]:
        """Distinct origin airports, in first-seen order.

        One provider call set is needed per origin, so this is also the multiplier
        on API quota.
        """
        seen: dict[str, None] = {}
        for origin, _ in self.routes:
            seen.setdefault(origin, None)
        return list(seen)

    @property
    def destinations_by_origin(self) -> dict[str, list[str]]:
        """Which destinations to keep from each origin's departure board.

        Filtering per origin rather than against one global destination set is what
        stops an unconfigured pair being collected: with routes IST-IKA and IKA-IST,
        a global set of {IST, IKA} would also accept IST-IST and, on a third
        airport's board, anything heading to either.
        """
        grouped: dict[str, list[str]] = {}
        for origin, destination in self.routes:
            grouped.setdefault(origin, [])
            if destination not in grouped[origin]:
                grouped[origin].append(destination)
        return grouped

    def route_labels(self) -> list[str]:
        """Routes as ``"IST-IKA"`` strings, for logs and API payloads."""
        return [f"{o}-{d}" for o, d in self.routes]

    @property
    def sync_database_url(self) -> str:
        """psycopg3 URL used by both SQLAlchemy and Alembic."""
        return self.database_url


def _parse_route(entry: str) -> tuple[str, str]:
    """Parse ``"IST-IKA"`` into ``("IST", "IKA")``."""
    origin, separator, destination = entry.strip().upper().partition("-")
    if not separator or not origin or not destination:
        raise ValueError(
            f"Route {entry!r} must be written as ORIGIN-DESTINATION, e.g. 'IST-IKA'."
        )
    if len(origin) != 3 or len(destination) != 3:
        raise ValueError(
            f"Route {entry!r} must use three-letter IATA codes, e.g. 'IKA-IST'."
        )
    return origin, destination


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
