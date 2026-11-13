"""Authorized, empty-target imports with atomic rows/sequences and reconciliation."""

import hashlib
import json
import re
from pathlib import Path

from psycopg import sql
from sqlalchemy import BigInteger, Boolean, Column, MetaData, Table, func, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError

from church_roster_bot.core.config import Settings
from church_roster_bot.core.database import Base, database_engine
from church_roster_bot.database.canonical import fingerprint, json_text, json_value, rows
from church_roster_bot.database.errors import TransferError
from church_roster_bot.database.schema import validate_postgres
from church_roster_bot.database.source import sqlite_snapshot


def _digest(value) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _engine(target_url, schema):
    if (
        not re.fullmatch(r"[a-z][a-z0-9_]{0,62}", schema)
        or schema.startswith("pg_")
        or schema == "information_schema"
    ):
        raise TransferError("invalid_target_schema")
    url = make_url(target_url)
    if url.drivername in {"postgres", "postgresql"}:
        url = url.set(drivername="postgresql+psycopg")
    if url.drivername != "postgresql+psycopg":
        raise TransferError("postgresql_target_required")
    native = str(url.query.get("options", "")) + " -csearch_path=" + schema
    config = Settings(
        _env_file=None,
        database_url=url.update_query_dict({"options": native}).render_as_string(
            hide_password=False
        ),
    )
    return database_engine(
        config, unpooled=True, json_serializer=json_text, json_deserializer=json_value
    )


def _identity(connection, schema):
    row = (
        connection.execute(
            text(
                "SELECT current_database() AS database, current_user AS actor, "
                "pg_postmaster_start_time() AS boot, inet_server_addr()::text AS address, "
                "inet_server_port() AS port, "
                "(SELECT oid::bigint FROM pg_database WHERE datname=current_database()) "
                "AS database_oid, "
                "(SELECT oid::bigint FROM pg_namespace WHERE nspname=:schema) AS schema_oid"
            ),
            {"schema": schema},
        )
        .mappings()
        .one()
    )
    return {**dict(row), "boot": row["boot"].isoformat(), "schema": schema}


def _proofs(connection):
    return {table.name: fingerprint(connection, table) for table in Base.metadata.sorted_tables}


def _sequences(connection, source, schema):
    plans = []
    preparer = connection.dialect.identifier_preparer
    for table in Base.metadata.sorted_tables:
        column = table.autoincrement_column
        if column is None:
            continue
        relation = preparer.quote_schema(schema) + "." + preparer.quote(table.name)
        row = (
            connection.execute(
                text(
                    "SELECT n.nspname, s.relname, p.seqmax, p.seqmin, p.seqincrement, p.seqcache, "
                    "p.seqcycle, pg_has_role(current_user,s.relowner,'USAGE') AS owned "
                    "FROM pg_class s JOIN pg_namespace n ON n.oid=s.relnamespace "
                    "JOIN pg_sequence p ON p.seqrelid=s.oid "
                    "WHERE s.oid=pg_get_serial_sequence(:relation,:column)::regclass"
                ),
                {"relation": relation, "column": column.name},
            )
            .mappings()
            .one_or_none()
        )
        if row is None or not row["owned"]:
            raise TransferError("migration_role_must_own_sequence", table=table.name)
        if (
            row["seqincrement"] != 1
            or row["seqmin"] != 1
            or row["seqcache"] != 1
            or row["seqcycle"]
        ):
            raise TransferError("unsupported_target_sequence", table=table.name)
        maximum = source.connection.scalar(select(func.max(column))) or 0
        next_value = max(maximum, source.sequence_floors.get(table.name, 0)) + 1
        if next_value > row["seqmax"]:
            raise TransferError("source_identity_range_exhausted", table=table.name)
        sequence = Table(
            row["relname"],
            MetaData(),
            Column("last_value", BigInteger),
            Column("is_called", Boolean),
            schema=row["nspname"],
        )
        state = dict(connection.execute(select(sequence)).mappings().one())
        plans.append(
            {
                "table": table.name,
                "schema": row["nspname"],
                "name": row["relname"],
                "next_value": next_value,
                "maximum": row["seqmax"],
                "before": state,
            }
        )
    return plans


def _verify_sequences(plans, *, exact=False):
    for plan in plans:
        state = plan["before"]
        actual_next = state["last_value"] + int(state["is_called"])
        if (
            actual_next < plan["next_value"]
            or actual_next > plan["maximum"]
            or (exact and actual_next != plan["next_value"])
        ):
            raise TransferError("sequence_reconciliation_failed", table=plan["table"])


def _lock(connection, schema, identity):
    key = int.from_bytes(
        hashlib.sha256((identity["database"] + ":" + schema).encode()).digest()[:8], signed=True
    )
    if not connection.scalar(text("SELECT pg_try_advisory_xact_lock(:key)"), {"key": key}):
        raise TransferError("another_import_is_running")
    names = ["alembic_version", *sorted(Base.metadata.tables)]
    statement = sql.SQL("LOCK TABLE {} IN EXCLUSIVE MODE").format(
        sql.SQL(",").join(sql.Identifier(schema, name) for name in names)
    )
    connection.exec_driver_sql(statement.as_string(connection.connection.driver_connection))


def transfer_sqlite(
    source_path: Path,
    target_url,
    *,
    schema: str,
    mode: str = "dry-run",
    confirmation: str | None = None,
    offline_confirmed: bool = False,
    upgrade_copy: bool = False,
) -> dict:
    if mode not in {"dry-run", "apply", "verify"}:
        raise TransferError("invalid_transfer_mode")
    if mode == "apply" and (not confirmation or not offline_confirmed):
        raise TransferError("explicit_offline_and_plan_confirmation_required")
    engine = _engine(target_url, schema)
    committed = False
    try:
        with sqlite_snapshot(source_path, upgrade_copy=upgrade_copy) as source:
            with engine.connect().execution_options(
                schema_translate_map={None: schema}
            ) as connection:
                transaction = connection.begin()
                try:
                    if mode != "apply":
                        connection.exec_driver_sql(
                            "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY"
                        )
                    validate_postgres(connection, schema)
                    identity = _identity(connection, schema)
                    if mode == "apply":
                        _lock(connection, schema, identity)
                        validate_postgres(connection, schema)
                    before = _proofs(connection)
                    if mode != "verify" and any(p["count"] for p in before.values()):
                        raise TransferError("target_must_be_empty")
                    sequence_plan = _sequences(connection, source, schema)
                    token = _digest(
                        {
                            "source": source.source_sha256,
                            "data": source.tables,
                            "revision": source.revision,
                            "target": identity,
                            "url": engine.url.render_as_string(hide_password=True),
                            "target_data": before,
                            "sequences": sequence_plan,
                        }
                    )
                    report = {
                        "status": "validated",
                        "mode": mode,
                        "revision": source.revision,
                        "source_sha256": source.source_sha256,
                        "original_revision": source.original_revision,
                        "upgraded_private_copy": source.upgraded_copy,
                        "original_source_tables": source.original_tables,
                        "source_tables": source.tables,
                        "target_database": identity["database"],
                        "target_schema": schema,
                        "target_identity_sha256": _digest(identity),
                        "target_before": before,
                        "sequence_plan": sequence_plan,
                        "plan_token": token,
                    }
                    if mode == "apply":
                        if confirmation != token:
                            raise TransferError("plan_confirmation_mismatch")
                        for table in Base.metadata.sorted_tables:
                            for batch in rows(source.connection, table):
                                connection.execute(table.insert(), batch)
                        after = _proofs(connection)
                        if after != source.tables:
                            raise TransferError("reconciliation_failed")
                        for plan in sequence_plan:
                            statement = sql.SQL("ALTER SEQUENCE {} RESTART WITH {}").format(
                                sql.Identifier(plan["schema"], plan["name"]),
                                sql.Literal(plan["next_value"]),
                            )
                            connection.exec_driver_sql(
                                statement.as_string(connection.connection.driver_connection)
                            )
                        validate_postgres(connection, schema)
                        report["sequence_after"] = _sequences(connection, source, schema)
                        _verify_sequences(report["sequence_after"], exact=True)
                        source.check_original()
                        report["target_after"] = after
                        report["status"] = "committed"
                        try:
                            transaction.commit()
                        except (DBAPIError, KeyboardInterrupt) as exc:
                            raise TransferError("commit_outcome_unknown_do_not_retry") from exc
                        committed = True
                    elif mode == "verify":
                        if before != source.tables:
                            raise TransferError("reconciliation_failed")
                        _verify_sequences(sequence_plan)
                        report["status"] = "verified-existing"
                        report["target_after"] = before
                        transaction.rollback()
                    else:
                        transaction.rollback()
                    return report
                finally:
                    if transaction.is_active:
                        transaction.rollback()
    except Exception as exc:
        if committed:
            raise TransferError("verification_failed_after_commit_do_not_retry") from exc
        raise
    finally:
        engine.dispose()
