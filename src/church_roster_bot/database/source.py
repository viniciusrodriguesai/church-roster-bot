"""Read-only SQLite snapshots; legacy upgrades affect a private copy only."""

import hashlib
import sqlite3
from contextlib import ExitStack, contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import quote

from sqlalchemy import MetaData, create_engine, inspect, text
from sqlalchemy.engine import URL, Connection
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.pool import NullPool

from alembic import command
from church_roster_bot.core.database import Base
from church_roster_bot.database.canonical import fingerprint
from church_roster_bot.database.errors import TransferError
from church_roster_bot.database.schema import (
    checked_revision,
    head_revision,
    migration_config,
    physical_schema,
    require_schema,
    revision_schema,
)


def file_hash(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def _identity(path: Path):
    stat = path.stat()
    return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns


def _offline(path: Path):
    if any(Path(str(path) + suffix).exists() for suffix in ("-wal", "-shm", "-journal")):
        raise TransferError("source_requires_offline_snapshot")
    with path.open("rb") as handle:
        header = handle.read(100)
    if len(header) != 100 or not header.startswith(b"SQLite format 3\x00"):
        raise TransferError("invalid_sqlite_file")
    if header[18:20] != b"\x01\x01":
        raise TransferError("source_requires_rollback_journal_snapshot")


def _check_integrity(connection):
    if connection.exec_driver_sql("PRAGMA integrity_check").scalars().all() != ["ok"]:
        raise TransferError("source_integrity_check_failed")
    if connection.exec_driver_sql("PRAGMA foreign_key_check").first() is not None:
        raise TransferError("source_foreign_key_violation")
    if connection.scalar(
        text("SELECT count(*) FROM sqlite_master WHERE type IN ('trigger','view')")
    ):
        raise TransferError("source_contains_unexpected_sql_objects")


def _revision(connection):
    if "alembic_version" not in inspect(connection).get_table_names():
        return None
    revisions = (
        connection.exec_driver_sql("SELECT version_num FROM alembic_version").scalars().all()
    )
    if len(revisions) != 1:
        raise TransferError("source_revision_required")
    return checked_revision(revisions[0])


@dataclass
class SQLiteSnapshot:
    connection: Connection = field(repr=False)
    path: Path = field(repr=False)
    identity: tuple = field(repr=False)
    source_sha256: str
    original_revision: str | None
    revision: str
    upgraded_copy: bool
    tables: dict
    original_tables: dict
    sequence_floors: dict

    def check_original(self):
        _offline(self.path)
        if _identity(self.path) != self.identity or file_hash(self.path) != self.source_sha256:
            raise TransferError("source_changed")


@contextmanager
def sqlite_snapshot(path: Path, *, upgrade_copy: bool = False):
    path = Path(path).resolve(strict=True)
    _offline(path)
    identity, digest = _identity(path), file_hash(path)
    with ExitStack() as stack:
        url = URL.create(
            "sqlite+pysqlite",
            database="file:" + quote(path.as_posix(), safe="/:"),
            query={"mode": "ro", "uri": "true"},
        )
        engine = create_engine(url, poolclass=NullPool, hide_parameters=True)
        stack.callback(engine.dispose)
        original = stack.enter_context(engine.connect())
        original.exec_driver_sql("PRAGMA query_only=ON")
        original.exec_driver_sql("BEGIN")
        _check_integrity(original)
        revision, head = _revision(original), head_revision()
        require_schema(physical_schema(original), revision_schema(revision or head))
        legacy = MetaData()
        legacy.reflect(bind=original)
        before = {
            table.name: fingerprint(original, table)
            for table in legacy.sorted_tables
            if table.name != "alembic_version"
        }
        prepared = original
        if revision is not None and revision != head:
            if not upgrade_copy:
                raise TransferError("source_upgrade_copy_required")
            temporary = Path(stack.enter_context(TemporaryDirectory(prefix="roster-transfer-")))
            copy_path = temporary / "prepared.sqlite3"
            with sqlite3.connect(copy_path) as target:
                original.connection.driver_connection.backup(target)
            copy_engine = create_engine(
                URL.create("sqlite+pysqlite", database=str(copy_path)), hide_parameters=True
            )
            stack.callback(copy_engine.dispose)
            prepared = stack.enter_context(copy_engine.connect())
            try:
                with prepared.begin():
                    prepared.exec_driver_sql("BEGIN IMMEDIATE")
                    config = migration_config()
                    config.attributes["connection"] = prepared
                    command.upgrade(config, "head")
            except SQLAlchemyError as exc:
                raise TransferError("source_copy_upgrade_failed") from exc
            for table in legacy.sorted_tables:
                if (
                    table.name != "alembic_version"
                    and fingerprint(prepared, table) != before[table.name]
                ):
                    raise TransferError("source_copy_changed_legacy_values", table=table.name)
            prepared.rollback()
            prepared.exec_driver_sql("PRAGMA query_only=ON")
            prepared.exec_driver_sql("BEGIN")
        _check_integrity(prepared)
        require_schema(physical_schema(prepared), revision_schema(head))
        proofs = {table.name: fingerprint(prepared, table) for table in Base.metadata.sorted_tables}
        floors = {}
        if original.scalar(text("SELECT count(*) FROM sqlite_master WHERE name='sqlite_sequence'")):
            for name, value in original.exec_driver_sql("SELECT name,seq FROM sqlite_sequence"):
                if (
                    name not in Base.metadata.tables
                    or type(value) is not int
                    or not 0 <= value < 2**31 - 1
                ):
                    raise TransferError("unsupported_source_sequence")
                floors[name] = value
        snapshot = SQLiteSnapshot(
            prepared,
            path,
            identity,
            digest,
            revision,
            head,
            prepared is not original,
            proofs,
            before,
            floors,
        )
        snapshot.check_original()
        try:
            yield snapshot
        finally:
            snapshot.check_original()
