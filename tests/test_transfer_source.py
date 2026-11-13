"""Source preparation must never write originals, guess schemas or invent legacy data."""

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError

from alembic import command
from church_roster_bot.core.database import Base
from church_roster_bot.database.errors import TransferError
from church_roster_bot.database.schema import head_revision, migration_config
from church_roster_bot.database.source import file_hash, sqlite_snapshot


def source_file(tmp_path, revision=None):
    path = tmp_path / "fictitious.sqlite3"
    engine = create_engine("sqlite:///" + path.as_posix())
    with engine.begin() as db:
        if revision:
            config = migration_config()
            config.attributes["connection"] = db
            command.upgrade(config, revision)
        else:
            Base.metadata.create_all(db)
        db.execute(text("INSERT INTO churches (id,name,timezone) VALUES (17,'Fictitious','UTC')"))
    engine.dispose()
    return path


@pytest.mark.parametrize("revision", [None, "head", "88ca949752d6", "a27b6c1d4e90", "231e4c9a7308"])
def test_original_is_readonly_and_legacy_copy_preserves_every_old_column(tmp_path, revision):
    path = source_file(tmp_path, revision)
    before = file_hash(path), path.stat().st_mtime_ns
    with sqlite_snapshot(path, upgrade_copy=True) as source:
        assert source.revision == head_revision()
        assert source.tables["churches"]["count"] == 1
        assert source.original_tables["churches"] == source.tables["churches"]
        with pytest.raises(OperationalError, match="readonly"):
            source.connection.execute(text("UPDATE churches SET name='not allowed'"))
        source.connection.rollback()
    assert (file_hash(path), path.stat().st_mtime_ns) == before


def test_legacy_upgrade_is_explicit_and_preserves_source_when_not_authorized(tmp_path):
    path = source_file(tmp_path, "88ca949752d6")
    before = file_hash(path)
    with pytest.raises(TransferError, match="source_upgrade_copy_required"), sqlite_snapshot(path):
        pass
    assert file_hash(path) == before


def test_missing_source_is_not_created(tmp_path):
    path = tmp_path / "missing.sqlite3"
    with pytest.raises(FileNotFoundError), sqlite_snapshot(path):
        pass
    assert not path.exists()


def test_wal_is_not_silently_ignored(tmp_path):
    path = source_file(tmp_path)
    sidecar = path.with_name(path.name + "-wal")
    sidecar.write_bytes(b"fictitious WAL requires a proper snapshot")
    before = file_hash(path)
    with pytest.raises(TransferError, match="offline_snapshot"), sqlite_snapshot(path):
        pass
    assert file_hash(path) == before and sidecar.exists()


def test_orphan_relationships_are_not_repaired_or_skipped(tmp_path):
    path = source_file(tmp_path)
    engine = create_engine("sqlite:///" + path.as_posix())
    try:
        with engine.begin() as db:
            db.execute(
                text("INSERT INTO ministries (id,church_id,name) VALUES (1,999,'Fictitious')")
            )
        with pytest.raises(TransferError, match="foreign_key"), sqlite_snapshot(path):
            pass
    finally:
        engine.dispose()
