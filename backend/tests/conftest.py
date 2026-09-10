"""Shared test fixtures.

Unit tests run with no external dependency. Integration tests need PostgreSQL and
are pointed at it with ``TEST_DATABASE_URL``; when it is absent they skip rather
than fail, so ``pytest`` is always runnable on a bare checkout.
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from app.analytics.metrics import FlightFact
from app.core.enums import FlightStatus

TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL")


@pytest.fixture(autouse=True)
def _isolate_dotenv(monkeypatch):
    """Stop a developer's local ``.env`` from leaking into the test suite.

    Two separate leaks have to be closed:

    1. ``Settings()`` constructed *during* a test reads ``.env``/``../.env``, so the
       env-file source is disabled.
    2. ``app.core.config.settings`` is a module-level singleton built at **import**
       time - before any fixture runs - and most modules hold a reference to that
       exact object. Patching the class alone leaves it populated with whatever the
       developer's ``.env`` happened to contain, which silently changed the outcome
       of the readiness test once a real provider key was configured locally. The
       credential fields on the shared instance are therefore reset too; because
       every module shares the object, the reset propagates.

    Tests that need a specific value set it explicitly with ``monkeypatch.setenv``
    or by patching their module's ``settings`` reference.
    """
    from app.core.config import Settings
    from app.core.config import settings as shared

    monkeypatch.setitem(Settings.model_config, "env_file", None)

    defaults = Settings(_env_file=None)
    for field in (
        "aerodatabox_api_key",
        "aviationstack_api_key",
        "flightaware_api_key",
        "telegram_bot_token",
        "telegram_chat_id",
        "provider_chain",
        "allow_mock_provider",
        "environment",
        "collection_interval_minutes",
        "collection_lookback_hours",
        "collection_lookahead_hours",
    ):
        monkeypatch.setattr(shared, field, getattr(defaults, field), raising=False)
    yield


# ------------------------------------------------------------------ unit data
@pytest.fixture
def make_fact():
    """Factory for :class:`FlightFact` values with sensible defaults."""
    counter = {"n": 0}

    def _make(
        *,
        delay: int | None = 0,
        cancelled: bool = False,
        diverted: bool = False,
        hour: int = 8,
        destination: str = "IKA",
        airline: str = "TK",
        flight_number: str | None = None,
        day: date | None = None,
        status: FlightStatus | None = None,
    ) -> FlightFact:
        counter["n"] += 1
        n = counter["n"]
        flight_day = day or date(2026, 9, 1)
        if status is None:
            status = FlightStatus.CANCELLED if cancelled else FlightStatus.DEPARTED
        return FlightFact(
            flight_id=n,
            flight_number=flight_number or f"{airline}{100 + n}",
            airline_iata=airline,
            airline_icao=None,
            airline_name={"TK": "Turkish Airlines", "W5": "Mahan Air"}.get(airline, airline),
            origin_iata="IST",
            destination_iata=destination,
            scheduled_departure_utc=datetime(
                flight_day.year, flight_day.month, flight_day.day, hour, tzinfo=UTC
            ),
            flight_date_local=flight_day,
            hour_local=hour,
            dow_local=flight_day.isoweekday(),
            status=status,
            delay_minutes=None if cancelled else delay,
            is_cancelled=cancelled,
            is_diverted=diverted,
        )

    return _make


@pytest.fixture
def sample_facts(make_fact) -> list[FlightFact]:
    """A small, deliberately mixed population used across analytics tests."""
    facts: list[FlightFact] = []
    # Morning: reliable.
    for _ in range(12):
        facts.append(make_fact(delay=5, hour=8))
    # Evening: bad - delays and cancellations.
    for _ in range(8):
        facts.append(make_fact(delay=75, hour=19))
    for _ in range(4):
        facts.append(make_fact(cancelled=True, hour=19))
    # One flight the provider gave no timings for.
    facts.append(make_fact(delay=None, hour=8))
    return facts


# ----------------------------------------------------------- integration DB
requires_db = pytest.mark.skipif(
    not TEST_DATABASE_URL,
    reason="TEST_DATABASE_URL is not set; skipping database integration tests",
)


@pytest.fixture(scope="session")
def engine():
    """A clean schema built by the real Alembic migrations.

    Migrations are used rather than ``create_all`` so the tests exercise the same
    DDL and seed data that production gets - a migration that drifts from the
    models will fail here rather than in deployment.
    """
    if not TEST_DATABASE_URL:
        pytest.skip("TEST_DATABASE_URL is not set")
    eng = create_engine(TEST_DATABASE_URL, future=True)

    with eng.begin() as conn:
        conn.execute(text("DROP SCHEMA public CASCADE; CREATE SCHEMA public;"))
    eng.dispose()

    from alembic import command
    from alembic.config import Config

    root = Path(__file__).resolve().parents[1]
    config = Config(str(root / "alembic.ini"))
    config.set_main_option("script_location", str(root / "migrations"))
    config.set_main_option("sqlalchemy.url", TEST_DATABASE_URL)
    command.upgrade(config, "head")

    eng = create_engine(TEST_DATABASE_URL, future=True)
    yield eng
    eng.dispose()


@pytest.fixture
def db_session(engine) -> Iterator[Session]:
    """A session wrapped in a transaction that is rolled back after each test."""
    connection = engine.connect()
    transaction = connection.begin()
    factory = sessionmaker(bind=connection, autoflush=False, expire_on_commit=False)
    session = factory()
    try:
        yield session
    finally:
        session.close()
        transaction.rollback()
        connection.close()


@pytest.fixture
def utc_now() -> datetime:
    return datetime(2026, 9, 6, 12, 0, tzinfo=UTC)


@pytest.fixture
def tomorrow() -> datetime:
    return datetime(2026, 9, 6, 12, 0, tzinfo=UTC) + timedelta(days=1)
