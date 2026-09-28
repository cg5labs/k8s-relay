"""Cron next-fire computation."""

from __future__ import annotations

from datetime import datetime

from croniter import croniter


def next_fire(schedule: str, after: datetime) -> datetime:
    """Return the first fire time strictly after ``after`` (timezone-aware)."""
    if after.tzinfo is None:
        raise ValueError("'after' must be timezone-aware")
    return croniter(schedule, after).get_next(datetime)
