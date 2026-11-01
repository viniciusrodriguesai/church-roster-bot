"""Canonicalization must preserve precision, nullness and timezone semantics."""

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import (
    JSON,
    Boolean,
    Column,
    DateTime,
    Integer,
    MetaData,
    String,
    Table,
    create_engine,
    text,
)

from church_roster_bot.database.canonical import fingerprint, json_text, json_value, typed_value
from church_roster_bot.database.errors import TransferError


def column(kind, **kwargs):
    table = Table("fictitious", MetaData(), Column("value", kind, **kwargs))
    return table.c.value


def test_json_precision_and_types_are_not_changed():
    raw = '{"number":0.123456789012345678901234567890,"integer":123,"null":null}'
    value = json_value(raw)
    assert value["number"] == Decimal("0.123456789012345678901234567890")
    assert type(value["integer"]) is int
    assert json_value(json_text(value)) == value
    assert '"number":0.123456789012345678901234567890' in json_text(value)


@pytest.mark.parametrize("raw", ['{"a":1,"a":2}', "NaN", "Infinity", '"\\u0000"', '"\\ud800"'])
def test_invalid_json_does_not_get_repaired_or_echoed(raw):
    with pytest.raises(TransferError) as caught:
        typed_value(column(JSON, nullable=False), raw)
    assert str(caught.value) == "incompatible_value"


@pytest.mark.parametrize("value", [2, -1, "true", 0.5])
def test_sqlite_boolean_coercion_is_rejected(value):
    with pytest.raises(TransferError):
        typed_value(column(Boolean), value)


def test_utc_conversion_preserves_the_instant_not_church_wall_time():
    value = typed_value(column(DateTime(timezone=True)), "2026-12-04T19:30:00-03:00")
    assert value == datetime(2026, 12, 4, 22, 30, tzinfo=UTC)
    assert typed_value(column(DateTime(timezone=True)), "2026-12-04T22:30:00") == value


@pytest.mark.parametrize("kind,value", [(Integer, 2**40), (String(3), "FICTITIOUS_SECRET")])
def test_sqlite_values_must_fit_postgresql_without_truncation(kind, value):
    with pytest.raises(TransferError) as caught:
        typed_value(column(kind), value)
    assert "FICTITIOUS_SECRET" not in str(caught.value)


def test_same_counts_are_not_sufficient_and_json_null_is_not_sql_null():
    table = Table(
        "fictitious", MetaData(), Column("id", Integer, primary_key=True), Column("data", JSON)
    )
    engine = create_engine("sqlite://")
    try:
        table.metadata.create_all(engine)
        with engine.begin() as db:
            db.execute(text("INSERT INTO fictitious VALUES (1, 'null')"))
            original = fingerprint(db, table)
            db.execute(text("UPDATE fictitious SET data=NULL"))
            changed = fingerprint(db, table)
            assert original["count"] == changed["count"] == 1
            assert original["sha256"] != changed["sha256"]
    finally:
        engine.dispose()
