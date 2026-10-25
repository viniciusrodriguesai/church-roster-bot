"""UTC boundaries shared by persisted tasks and SQLite simulation."""

from datetime import UTC, date, datetime, time
from zoneinfo import ZoneInfo


def utc_now(value: datetime | None = None) -> datetime:
    instant = value if value is not None else datetime.now(UTC)
    if instant.utcoffset() is None:
        raise ValueError("an explicit clock must include a timezone")
    return instant.astimezone(UTC)


def database_utc(value: datetime) -> datetime:
    """SQLite returns naive datetimes for values that were written as UTC."""
    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


def local_to_utc(day: date, start: time, timezone: str) -> datetime:
    zone = ZoneInfo(timezone)
    local = datetime.combine(day, start, tzinfo=zone)
    instant = local.astimezone(UTC)
    if instant.astimezone(zone).replace(tzinfo=None) != local.replace(tzinfo=None):
        raise ValueError("service time does not exist in the configured timezone")
    # Ambiguous wall times consistently use the first occurrence (fold=0).
    return instant
