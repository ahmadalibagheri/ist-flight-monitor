"""Seed-dataset import.

A fresh checkout has no observation history, so every analytic is empty and the
dashboard can only say "nothing collected yet". This module imports a shipped
snapshot of real observations so the first run has something to compute from.

Design constraints this respects:

* **Import only into an empty database.** The seed is a starting point, never an
  overlay. If any flight already exists the import is skipped, so it can never
  interleave with — or overwrite — data a deployment collected itself.
* **The statistics tables are not shipped.** They are a rebuildable cache over
  the observations, so the import recomputes them. Shipping them would create a
  second source of truth that could disagree with the rows beneath it.
* **Mock rows never enter.** Anything tagged ``is_mock`` in the source export is
  discarded here, so a development export cannot smuggle synthetic flights into
  someone else's analytics.
* **Sequences are advanced afterwards.** The dump carries explicit primary keys;
  without resetting the identity sequences the first locally-collected flight
  would collide with a seeded id.
"""

from __future__ import annotations

import gzip
from dataclasses import dataclass
from importlib import resources
from pathlib import Path

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.logging_config import get_logger
from app.models import Airline, CollectionRun, Flight, FlightEvent, FlightObservation

log = get_logger(__name__)

SEED_FILENAME = "seed_dataset.sql.gz"

#: Tables the seed populates, in dependency order.
SEEDED_TABLES: tuple[str, ...] = (
    "airlines",
    "collection_runs",
    "flights",
    "flight_observations",
    "flight_events",
)


class SeedError(RuntimeError):
    """The seed dataset is missing or could not be applied."""


@dataclass(slots=True)
class SeedResult:
    applied: bool
    reason: str | None = None
    rows: dict[str, int] | None = None

    def as_dict(self) -> dict[str, object]:
        return {"applied": self.applied, "reason": self.reason, "rows": self.rows or {}}


def seed_path() -> Path:
    """Locate the shipped dataset inside the installed package."""
    with resources.as_file(resources.files("app.data") / SEED_FILENAME) as path:
        return Path(path)


def seed_available() -> bool:
    try:
        return seed_path().is_file()
    except (ModuleNotFoundError, FileNotFoundError):
        return False


def database_is_empty(session: Session) -> bool:
    """True when no flight has ever been recorded, mock or otherwise."""
    return (session.scalar(select(func.count()).select_from(Flight)) or 0) == 0


def import_seed(session: Session, *, force: bool = False) -> SeedResult:
    """Load the shipped observation history.

    Args:
        force: Import even if flights already exist. Existing rows are left
            alone; conflicting primary keys are skipped rather than overwritten,
            because an observation already on disk is the authoritative one.

    Returns:
        A :class:`SeedResult` describing what happened. A skip is not an error -
        on every run after the first, skipping is the correct outcome.
    """
    if not seed_available():
        return SeedResult(applied=False, reason="no seed dataset is bundled")

    if not force and not database_is_empty(session):
        return SeedResult(
            applied=False,
            reason="database already holds flights; seed applies only to an empty one",
        )

    sql = _strip_psql_directives(gzip.decompress(seed_path().read_bytes()).decode("utf-8"))
    if not sql.strip():
        raise SeedError("seed dataset is empty")

    log.info("seed.importing", source=str(seed_path()), bytes=len(sql))

    # Plain INSERT statements (see scripts/export-seed.sh), executed inside the
    # caller's transaction so a failure part-way leaves nothing behind.
    session.execute(text(sql))

    # pg_dump blanks search_path so its own statements are unambiguous. That setting
    # outlives the script, and every unqualified query after it — ours and the ORM's —
    # would fail with "relation does not exist". Restore it before doing anything else.
    session.execute(text("SELECT pg_catalog.set_config('search_path', 'public', false)"))

    _discard_mock_rows(session)
    _reset_sequences(session)
    session.flush()

    rows = {
        "airlines": session.scalar(select(func.count()).select_from(Airline)) or 0,
        "collection_runs": session.scalar(select(func.count()).select_from(CollectionRun)) or 0,
        "flights": session.scalar(select(func.count()).select_from(Flight)) or 0,
        "flight_observations": (
            session.scalar(select(func.count()).select_from(FlightObservation)) or 0
        ),
        "flight_events": session.scalar(select(func.count()).select_from(FlightEvent)) or 0,
    }
    log.info("seed.imported", **rows)
    return SeedResult(applied=True, rows=rows)


def _strip_psql_directives(sql: str) -> str:
    """Remove lines only ``psql`` understands.

    ``pg_dump`` emits meta-commands such as ``\\restrict`` alongside the SQL. They
    are not statements, so any real driver rejects the whole script with a syntax
    error. The export script filters them too; this is the belt-and-braces pass so
    a dataset produced by a different pg_dump version still imports.
    """
    return "\n".join(
        line for line in sql.splitlines() if not line.startswith("\\")
    )


def _discard_mock_rows(session: Session) -> None:
    """Drop any synthetic flight the export happened to contain.

    A development export can legitimately include mock rows; they must not become
    someone else's history. Deleting the flights cascades to their observations
    and events.
    """
    result = session.execute(text("DELETE FROM flights WHERE is_mock = true"))
    deleted = getattr(result, "rowcount", 0) or 0
    if deleted:
        log.warning("seed.mock_rows_discarded", count=deleted)


def _reset_sequences(session: Session) -> None:
    """Advance identity sequences past the imported primary keys.

    The dump carries explicit ids. Without this, the next locally-collected
    flight would be assigned id 1 and collide.
    """
    for table in SEEDED_TABLES:
        # airlines uses an autoincrement id; the others use bigint identities.
        session.execute(
            text(
                "SELECT setval(pg_get_serial_sequence(:tbl, 'id'), "
                "GREATEST(COALESCE((SELECT MAX(id) FROM " + table + "), 0), 1)) "
                "WHERE pg_get_serial_sequence(:tbl, 'id') IS NOT NULL"
            ),
            {"tbl": table},
        )


def seed_if_empty(session: Session) -> SeedResult:
    """Entry point for startup: import only when there is nothing to lose.

    Controlled by ``SEED_ON_EMPTY``. Disabled automatically in production, where
    an empty database means "not collecting yet", not "needs demo data".
    """
    if not settings.seed_on_empty:
        return SeedResult(applied=False, reason="SEED_ON_EMPTY is false")
    if settings.environment == "production":
        return SeedResult(
            applied=False,
            reason="seeding is disabled in production; collect your own history",
        )
    return import_seed(session)


def rebuild_statistics(session: Session, days: int = 60) -> int:
    """Recompute the statistics cache over the imported window.

    The seed deliberately ships no statistics rows, so this is what makes the
    dashboard's charts populate after an import.
    """
    from datetime import timedelta

    from app.analytics.aggregator import rebuild_range
    from app.analytics.queries import local_today

    end = local_today()
    results = rebuild_range(session, end - timedelta(days=days - 1), end)
    return sum(int(r.get("daily_rows", 0)) for r in results)
