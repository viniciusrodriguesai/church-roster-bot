"""Lossless typed rows and deterministic fingerprints across SQLite/PostgreSQL."""

import hashlib
import json
from datetime import UTC, date, datetime, time
from decimal import Decimal

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    Integer,
    String,
    Text,
    Time,
    cast,
    null,
    select,
    type_coerce,
)
from sqlalchemy.engine import Connection
from sqlalchemy.sql.elements import Null
from sqlalchemy.sql.schema import Column, Table
from sqlalchemy.sql.sqltypes import NullType

from church_roster_bot.database.errors import TransferError


def json_text(value: object) -> str:
    """Decimal numbers remain JSON numbers, never rounded floats or strings."""
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if type(value) is int:
        return str(value)
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("non-finite JSON number")
        return str(value)
    if isinstance(value, str):
        value.encode("utf-8", errors="strict")
        if "\0" in value:
            raise ValueError("NUL is not portable")
        return json.dumps(value, ensure_ascii=True)
    if isinstance(value, list):
        return "[" + ",".join(json_text(item) for item in value) + "]"
    if isinstance(value, dict) and all(isinstance(key, str) for key in value):
        return (
            "{"
            + ",".join(json_text(key) + ":" + json_text(value[key]) for key in sorted(value))
            + "}"
        )
    raise ValueError("unsupported JSON value")


def _pairs(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON key")
        value[key] = item
    return value


def _invalid_constant(value):
    raise ValueError("non-finite JSON number")


def json_value(value: str):
    parsed = json.loads(
        value, parse_float=Decimal, parse_constant=_invalid_constant, object_pairs_hook=_pairs
    )
    json_text(parsed)  # Validate recursive strings, including escaped NUL/surrogates.
    return parsed


def typed_value(column: Column, value: object):
    """Reject SQLite coercions which PostgreSQL or the application cannot preserve."""
    try:
        if value is None:
            if not column.nullable:
                raise ValueError("null in required field")
            return null() if isinstance(column.type, JSON) else None
        if isinstance(column.type, JSON):
            if not isinstance(value, str):
                raise ValueError("JSON text required")
            parsed = json_value(value)
            return JSON.NULL if parsed is None else parsed
        if isinstance(column.type, Boolean):
            if type(value) not in {bool, int} or value not in (0, 1):
                raise ValueError("boolean must be zero or one")
            return bool(value)
        if isinstance(column.type, Integer):
            if type(value) is not int or not -(2**31) <= value <= 2**31 - 1:
                raise ValueError("INTEGER range or type mismatch")
            if column.primary_key and value < 1:
                raise ValueError("positive identity required")
            if column is column.table.autoincrement_column and value == 2**31 - 1:
                raise ValueError("identity sequence exhausted")
            return value
        if isinstance(column.type, DateTime):
            parsed = datetime.fromisoformat(value) if isinstance(value, str) else value
            if not isinstance(parsed, datetime):
                raise ValueError("datetime required")
            return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed.astimezone(UTC)
        if isinstance(column.type, Date):
            parsed = date.fromisoformat(value) if isinstance(value, str) else value
            if type(parsed) is not date:
                raise ValueError("civil date required")
            return parsed
        if isinstance(column.type, Time):
            parsed = time.fromisoformat(value) if isinstance(value, str) else value
            if not isinstance(parsed, time) or parsed.tzinfo is not None:
                raise ValueError("civil time without timezone required")
            return parsed
        if isinstance(column.type, String):
            if not isinstance(value, str) or "\0" in value:
                raise ValueError("portable text required")
            value.encode("utf-8", errors="strict")
            if column.type.length is not None and len(value) > column.type.length:
                raise ValueError("text exceeds declared size")
            return value
        raise ValueError("unsupported mapped type")
    except (ValueError, TypeError, OverflowError, RecursionError) as exc:
        raise TransferError(
            "incompatible_value", table=column.table.name, column=column.name
        ) from exc


def rows(connection: Connection, table: Table, *, batch_size: int = 500):
    expressions = []
    for column in table.columns:
        if not isinstance(column.type, (JSON, Boolean, Integer, DateTime, Date, Time, String)):
            raise TransferError("unsupported_mapped_type", table=table.name, column=column.name)
        expression = (
            cast(column, Text)
            if connection.dialect.name == "postgresql" and isinstance(column.type, JSON)
            else type_coerce(column, NullType())
        )
        expressions.append(expression.label(column.name))
    result = connection.execute(select(*expressions).order_by(*table.primary_key.columns))
    try:
        for batch in result.mappings().partitions(batch_size):
            yield [
                {column.name: typed_value(column, row[column.name]) for column in table.columns}
                for row in batch
            ]
    finally:
        result.close()


def _cell(column: Column, value):
    if value is None or isinstance(value, Null):
        return ["sql_null"]
    if isinstance(column.type, JSON):
        return ["json", json_text(None if value is JSON.NULL else value)]
    if isinstance(value, datetime):
        return ["utc", value.astimezone(UTC).isoformat(timespec="microseconds")]
    if isinstance(value, time):
        return ["time", value.isoformat(timespec="microseconds")]
    if isinstance(value, date):
        return ["date", value.isoformat()]
    kind = (
        "boolean"
        if isinstance(column.type, Boolean)
        else "integer"
        if isinstance(column.type, Integer)
        else "text"
    )
    return [kind, value]


def fingerprint(connection: Connection, table: Table) -> dict:
    digest = hashlib.sha256()
    count = 0
    for batch in rows(connection, table):
        for row in batch:
            payload = [[column.name, _cell(column, row[column.name])] for column in table.columns]
            digest.update(json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode())
            digest.update(b"\n")
            count += 1
    return {"count": count, "sha256": digest.hexdigest()}
