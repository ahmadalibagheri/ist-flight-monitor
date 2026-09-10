"""Shared FastAPI dependencies."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Annotated

from fastapi import Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.analytics.queries import local_today
from app.core.db import get_db

DbSession = Annotated[Session, Depends(get_db)]

#: Named rolling windows the API accepts wherever a period is selected.
WINDOWS: dict[str, int | None] = {
    "today": 1,
    "7d": 7,
    "30d": 30,
    "90d": 90,
    "mtd": None,     # month to date
    "all": None,     # everything ever collected
}


def resolve_window(
    window: Annotated[
        str,
        Query(
            description="Named window: today, 7d, 30d, 90d, mtd, all.",
        ),
    ] = "30d",
    start_date: Annotated[date | None, Query(description="Overrides `window`.")] = None,
    end_date: Annotated[date | None, Query(description="Defaults to today.")] = None,
) -> tuple[date, date, str]:
    """Turn window/date-range query parameters into a concrete local date range."""
    today = local_today()

    if start_date or end_date:
        start = start_date or (end_date or today) - timedelta(days=29)
        end = end_date or today
        if start > end:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="start_date must not be after end_date",
            )
        return start, end, f"{start}..{end}"

    key = window.lower()
    if key not in WINDOWS:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unknown window '{window}'. Valid: {', '.join(WINDOWS)}",
        )

    if key == "mtd":
        return today.replace(day=1), today, key
    if key == "all":
        # Far enough back to cover any realistic history without an extra query.
        return date(2000, 1, 1), today, key

    days = WINDOWS[key]
    assert days is not None
    return today - timedelta(days=days - 1), today, key


Window = Annotated[tuple[date, date, str], Depends(resolve_window)]


def pagination(
    limit: Annotated[int, Query(ge=1, le=500)] = 100,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> tuple[int, int]:
    return limit, offset


Pagination = Annotated[tuple[int, int], Depends(pagination)]
