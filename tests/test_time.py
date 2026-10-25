"""Clock and civil-time boundaries are deterministic across databases and DST."""

from datetime import UTC, date, datetime, time

import pytest

from church_roster_bot.core.time import database_utc, local_to_utc, utc_now


def test_explicit_clocks_require_an_offset_and_database_values_are_utc():
    with pytest.raises(ValueError, match="timezone"):
        utc_now(datetime(2027, 1, 1))
    assert database_utc(datetime(2027, 1, 1)) == datetime(2027, 1, 1, tzinfo=UTC)
    assert local_to_utc(date(2027, 1, 1), time(19), "America/Sao_Paulo") == datetime(
        2027, 1, 1, 22, tzinfo=UTC
    )


def test_nonexistent_dst_times_are_rejected_and_ambiguous_times_use_first_occurrence():
    with pytest.raises(ValueError, match="does not exist"):
        local_to_utc(date(2027, 3, 14), time(2, 30), "America/New_York")
    assert local_to_utc(date(2027, 11, 7), time(1, 30), "America/New_York") == datetime(
        2027, 11, 7, 5, 30, tzinfo=UTC
    )
