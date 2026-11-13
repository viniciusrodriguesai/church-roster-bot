"""Check actual schema enforcement instead of trusting an Alembic stamp alone."""

import pytest
from sqlalchemy import create_engine, text

from alembic import command
from church_roster_bot.database.errors import TransferError
from church_roster_bot.database.schema import (
    head_revision,
    migration_config,
    model_schema,
    physical_schema,
    require_schema,
    revision_schema,
    sql_signature,
    validate_postgres,
)


def test_constraint_normalization_never_erases_boolean_grouping_or_literal_content():
    with pytest.raises(TransferError, match="unsupported_schema_expression"):
        sql_signature("(month >= 1 OR month <= 12) AND month <> 0")
    assert sql_signature("status IN ('or', '::text')") != sql_signature("status IN ('or', '')")


def test_current_sqlite_metadata_matches_the_current_migration():
    engine = create_engine("sqlite://")
    try:
        with engine.begin() as db:
            config = migration_config()
            config.attributes["connection"] = db
            command.upgrade(config, "head")
            require_schema(physical_schema(db), model_schema(db))
            assert db.scalar(text("SELECT 1")) == 1  # Supplied connection remains usable.
    finally:
        engine.dispose()


@pytest.mark.parametrize("revision", ["88ca949752d6", "a27b6c1d4e90", "231e4c9a7308"])
def test_supported_older_schemas_are_built_without_a_source_database(revision):
    assert "churches" in revision_schema(revision)
    assert revision_schema(revision) != revision_schema(head_revision())


@pytest.mark.postgres
@pytest.mark.parametrize("migration_url", ["postgres"], indirect=True)
def test_postgresql_head_constraints_are_compared_and_disabled_checks_rejected(migration_url):
    engine = create_engine(migration_url)
    try:
        with engine.begin() as db:
            config = migration_config()
            config.attributes["connection"] = db
            command.upgrade(config, "head")
            schema = db.scalar(text("SELECT current_schema()"))
            validate_postgres(db, schema)
            db.execute(text("ALTER TABLE slots DROP CONSTRAINT positive_slot_capacity"))
            db.execute(
                text("ALTER TABLE slots ADD CONSTRAINT positive_slot_capacity CHECK (capacity>=0)")
            )
            with pytest.raises(TransferError, match="schema_drift"):
                validate_postgres(db, schema)
    finally:
        engine.dispose()
