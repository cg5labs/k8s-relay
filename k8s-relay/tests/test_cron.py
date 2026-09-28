from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

from lease_scheduler.cron import next_fire


def test_next_fire_is_strictly_after():
    now = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    assert next_fire("*/5 * * * *", now) == datetime(
        2026, 1, 1, 12, 5, tzinfo=timezone.utc
    )


def test_next_fire_respects_timezone():
    tz = ZoneInfo("Asia/Singapore")
    now = datetime(2026, 1, 1, 8, 30, tzinfo=tz)
    assert next_fire("0 9 * * *", now) == datetime(2026, 1, 1, 9, 0, tzinfo=tz)


def test_naive_datetime_rejected():
    with pytest.raises(ValueError):
        next_fire("* * * * *", datetime(2026, 1, 1))
