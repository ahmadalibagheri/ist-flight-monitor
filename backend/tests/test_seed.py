"""Seed-dataset import.

The dangerous failure here is not a broken import — that is loud. It is a seed
that silently overwrites or interleaves with data a deployment collected itself,
so most of these tests are about the import declining to run.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from sqlalchemy import func, select

from app.core.config import Settings
from app.core.enums import FlightStatus
from app.models import Flight
from app.services.seed import (
    SeedResult,
    _strip_psql_directives,
    database_is_empty,
    import_seed,
    seed_available,
    seed_if_empty,
    seed_path,
)
from tests.conftest import requires_db


class TestBundling:
    def test_the_dataset_ships_with_the_package(self) -> None:
        """Addressed through importlib.resources, so it survives packaging."""
        assert seed_available()
        assert seed_path().is_file()
        assert seed_path().suffix == ".gz"

    def test_psql_meta_commands_are_stripped(self) -> None:
        """pg_dump emits directives only psql understands; a driver rejects them."""
        raw = "\n".join(
            [
                "SET client_encoding = 'UTF8';",
                "\\restrict abc123",
                "INSERT INTO public.airlines (iata) VALUES ('TK');",
                "\\unrestrict abc123",
            ]
        )
        cleaned = _strip_psql_directives(raw)
        assert "\\restrict" not in cleaned
        assert "\\unrestrict" not in cleaned
        assert "INSERT INTO public.airlines" in cleaned

    def test_the_bundled_dump_carries_no_psql_directives(self) -> None:
        """The export script filters them; this asserts the committed file is clean."""
        import gzip

        sql = gzip.decompress(seed_path().read_bytes()).decode()
        offenders = [ln for ln in sql.splitlines() if ln.startswith("\\")]
        assert offenders == [], f"committed dataset needs stripping: {offenders[:3]}"


@requires_db
class TestImportGuards:
    def test_imports_into_an_empty_database(self, db_session) -> None:
        assert database_is_empty(db_session)
        result = import_seed(db_session)
        assert result.applied is True
        assert result.rows is not None
        assert result.rows["flights"] > 0
        assert result.rows["flight_observations"] > result.rows["flights"], (
            "the point of the seed is observation history, not just flight rows"
        )

    def test_declines_when_flights_already_exist(self, db_session) -> None:
        """The seed is a starting point, never an overlay."""
        db_session.add(
            Flight(
                flight_number="TK878",
                origin_iata="IST",
                destination_iata="IKA",
                scheduled_departure_utc=datetime(2026, 9, 6, 5, 15, tzinfo=UTC),
                flight_date_local=datetime(2026, 9, 6).date(),
                scheduled_hour_local=8,
                scheduled_dow_local=7,
                status=FlightStatus.SCHEDULED,
                is_cancelled=False,
                is_diverted=False,
                data_source="test-provider",
                is_mock=False,
            )
        )
        db_session.flush()

        result = import_seed(db_session)
        assert result.applied is False
        assert result.reason is not None
        assert "already holds flights" in result.reason
        # The pre-existing row is untouched.
        assert db_session.scalar(select(func.count()).select_from(Flight)) == 1

    def test_second_import_is_a_no_op(self, db_session) -> None:
        first = import_seed(db_session)
        assert first.applied is True
        before = db_session.scalar(select(func.count()).select_from(Flight))

        second = import_seed(db_session)
        assert second.applied is False
        assert db_session.scalar(select(func.count()).select_from(Flight)) == before

    def test_search_path_survives_the_import(self, db_session) -> None:
        """pg_dump blanks search_path; unqualified ORM queries fail if it is left so."""
        import_seed(db_session)
        # This is an unqualified query - it only works if search_path was restored.
        assert db_session.scalar(select(func.count()).select_from(Flight)) > 0

    def test_identity_sequences_do_not_collide_with_seeded_ids(self, db_session) -> None:
        """The dump carries explicit ids, so the sequence must be advanced past them."""
        import_seed(db_session)
        max_seeded = db_session.scalar(select(func.max(Flight.id)))
        assert max_seeded is not None

        fresh = Flight(
            flight_number="ZZ999",
            origin_iata="IST",
            destination_iata="IKA",
            scheduled_departure_utc=datetime(2026, 9, 20, 5, 15, tzinfo=UTC),
            flight_date_local=datetime(2026, 9, 20).date(),
            scheduled_hour_local=8,
            scheduled_dow_local=7,
            status=FlightStatus.SCHEDULED,
            is_cancelled=False,
            is_diverted=False,
            data_source="test-provider",
            is_mock=False,
        )
        db_session.add(fresh)
        db_session.flush()
        assert fresh.id > max_seeded, "a locally-collected flight collided with a seeded id"

    def test_no_mock_rows_enter(self, db_session) -> None:
        """A development export must not smuggle synthetic flights into analytics."""
        import_seed(db_session)
        mock_count = db_session.scalar(
            select(func.count()).select_from(Flight).where(Flight.is_mock.is_(True))
        )
        assert mock_count == 0

    def test_imported_history_is_usable_by_analytics(self, db_session) -> None:
        """The seed exists so analytics have something to compute from."""
        from app.analytics.aggregator import summarise
        from app.analytics.queries import facts_for_local_dates

        import_seed(db_session)
        earliest = db_session.scalar(select(func.min(Flight.flight_date_local)))
        latest = db_session.scalar(select(func.max(Flight.flight_date_local)))
        assert earliest is not None and latest is not None

        facts = facts_for_local_dates(db_session, earliest, latest)
        assert facts, "seeded flights are invisible to the analytics layer"
        payload = summarise(facts)
        assert payload["total_flights"] == len(facts)
        # Real data, so there is something to measure.
        assert payload["measurable_flights"] > 0


@requires_db
class TestStartupBehaviour:
    def test_disabled_by_the_flag(self, db_session, monkeypatch) -> None:
        import app.services.seed as seed_module

        monkeypatch.setattr(seed_module, "settings", Settings(seed_on_empty=False))
        result = seed_if_empty(db_session)
        assert result.applied is False
        assert result.reason is not None
        assert "SEED_ON_EMPTY" in result.reason

    def test_never_seeds_production(self, db_session, monkeypatch) -> None:
        """In production an empty database means "not collecting yet", not "needs data"."""
        import app.services.seed as seed_module

        monkeypatch.setattr(
            seed_module,
            "settings",
            Settings(environment="production", seed_on_empty=True),
        )
        result = seed_if_empty(db_session)
        assert result.applied is False
        assert result.reason is not None
        assert "production" in result.reason
        assert database_is_empty(db_session)

    def test_seeds_a_fresh_development_database(self, db_session, monkeypatch) -> None:
        import app.services.seed as seed_module

        monkeypatch.setattr(
            seed_module,
            "settings",
            Settings(environment="development", seed_on_empty=True),
        )
        assert seed_if_empty(db_session).applied is True


class TestResultShape:
    @pytest.mark.parametrize(
        ("result", "expected_applied"),
        [(SeedResult(applied=True, rows={"flights": 3}), True), (SeedResult(applied=False), False)],
    )
    def test_serialises_for_the_cli(self, result: SeedResult, expected_applied: bool) -> None:
        payload = result.as_dict()
        assert payload["applied"] is expected_applied
        assert isinstance(payload["rows"], dict)
