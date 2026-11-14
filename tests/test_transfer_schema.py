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
    literal = '\'"length(trim", "LENGTH", "any"\''
    assert literal in sql_signature(f"status IN ({literal})")


@pytest.mark.parametrize(
    "expression",
    [
        '"length(trim"(justification) > 0',
        '"LENGTH"(trim(justification)) > 0',
        "status = \"any\"(ARRAY['pending', 'accepted'])",
    ],
    ids=["quoted_structure", "quoted_uppercase", "quoted_keyword"],
)
def test_constraint_normalization_rejects_unsupported_quoted_identifiers(expression):
    with pytest.raises(TransferError, match="unsupported_schema_expression"):
        sql_signature(expression)
    assert sql_signature('"capacity" > 0') == sql_signature("capacity > 0")


def test_constraint_normalization_distinguishes_a_function_call_from_a_column():
    assert sql_signature("capacity() > 0") != sql_signature("capacity > 0")
    assert sql_signature("(capacity > 0) AND (capacity < 4)") == sql_signature(
        "capacity > 0 AND capacity < 4"
    )
    assert sql_signature("status IN ('pending', 'accepted')") == sql_signature(
        "status = ANY (ARRAY['pending'::text, 'accepted'::text])"
    )


def test_constraint_normalization_distinguishes_nested_from_concatenated_function_names():
    expected = sql_signature("length(trim(justification)) > 0")
    assert sql_signature("lengthtrim(justification) > 0") != expected
    assert sql_signature("length(btrim(justification::text)) > 0") == expected
    assert sql_signature("length(TRIM(BOTH FROM justification)) > 0") == expected


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


@pytest.mark.postgres
@pytest.mark.parametrize("migration_url", ["postgres"], indirect=True)
@pytest.mark.parametrize("setting", ["ENABLE", "FORCE"])
def test_postgresql_row_security_is_rejected(migration_url, setting):
    engine = create_engine(migration_url)
    try:
        with engine.begin() as db:
            config = migration_config()
            config.attributes["connection"] = db
            command.upgrade(config, "head")
            schema = db.scalar(text("SELECT current_schema()"))
            db.execute(text(f"ALTER TABLE churches {setting} ROW LEVEL SECURITY"))
            with pytest.raises(TransferError, match="unexpected_row_security"):
                validate_postgres(db, schema)
    finally:
        engine.dispose()


@pytest.mark.postgres
@pytest.mark.parametrize("migration_url", ["postgres"], indirect=True)
@pytest.mark.parametrize(
    "alterations",
    [
        ["ALTER TABLE churches ALTER COLUMN id DROP DEFAULT"],
        ["ALTER TABLE churches ALTER COLUMN id SET DEFAULT 1"],
        ["ALTER TABLE churches ALTER COLUMN id SET DEFAULT nextval('ministries_id_seq')"],
        [
            "ALTER TABLE churches ALTER COLUMN id DROP DEFAULT",
            "ALTER TABLE churches ALTER COLUMN id ADD GENERATED BY DEFAULT AS IDENTITY",
        ],
        ["ALTER TABLE churches ALTER COLUMN name SET DEFAULT 'Unexpected'"],
        [
            "ALTER TABLE churches DROP COLUMN name",
            "ALTER TABLE churches ADD COLUMN name varchar(160) "
            "GENERATED ALWAYS AS ('Unexpected'::varchar(160)) STORED NOT NULL",
        ],
    ],
    ids=["missing", "constant", "wrong_sequence", "identity", "extra_default", "computed"],
)
def test_postgresql_column_generators_must_match_current_models(migration_url, alterations):
    engine = create_engine(migration_url)
    try:
        with engine.begin() as db:
            config = migration_config()
            config.attributes["connection"] = db
            command.upgrade(config, "head")
            schema = db.scalar(text("SELECT current_schema()"))
            for statement in alterations:
                db.execute(text(statement))
            with pytest.raises(TransferError, match="unexpected_column_generator"):
                validate_postgres(db, schema)
    finally:
        engine.dispose()


@pytest.mark.postgres
@pytest.mark.parametrize("migration_url", ["postgres"], indirect=True)
@pytest.mark.parametrize(
    "table,constraint,expression,function",
    [
        (
            "slots",
            "positive_slot_capacity",
            "capacity() > 0",
            "CREATE FUNCTION capacity() RETURNS integer LANGUAGE sql IMMUTABLE AS 'SELECT 1'",
        ),
        (
            "third_day_requests",
            "nonempty_exception_justification",
            "lengthtrim(justification) > 0",
            "CREATE FUNCTION lengthtrim(text) RETURNS integer LANGUAGE sql IMMUTABLE AS 'SELECT 1'",
        ),
    ],
    ids=["column_as_function", "concatenated_function_names"],
)
def test_postgresql_function_calls_cannot_replace_column_checks(
    migration_url, table, constraint, expression, function
):
    engine = create_engine(migration_url)
    try:
        with engine.begin() as db:
            config = migration_config()
            config.attributes["connection"] = db
            command.upgrade(config, "head")
            schema = db.scalar(text("SELECT current_schema()"))
            validate_postgres(db, schema)
            db.execute(text(function))
            db.execute(text(f"ALTER TABLE {table} DROP CONSTRAINT {constraint}"))
            db.execute(
                text(f"ALTER TABLE {table} ADD CONSTRAINT {constraint} CHECK ({expression})")
            )
            with pytest.raises(TransferError, match="schema_drift"):
                validate_postgres(db, schema)
    finally:
        engine.dispose()


@pytest.mark.postgres
@pytest.mark.parametrize("migration_url", ["postgres"], indirect=True)
def test_postgresql_check_cannot_bind_to_a_user_defined_builtin_overload(migration_url):
    engine = create_engine(migration_url)
    try:
        with engine.begin() as db:
            config = migration_config()
            config.attributes["connection"] = db
            command.upgrade(config, "head")
            schema = db.scalar(text("SELECT current_schema()"))
            validate_postgres(db, schema)
            db.execute(
                text(
                    "CREATE FUNCTION btrim(varchar) RETURNS text LANGUAGE sql IMMUTABLE "
                    "AS $$SELECT 'X'::text$$"
                )
            )
            db.execute(
                text(
                    "ALTER TABLE third_day_requests "
                    "DROP CONSTRAINT nonempty_exception_justification"
                )
            )
            db.execute(
                text(
                    "ALTER TABLE third_day_requests "
                    "ADD CONSTRAINT nonempty_exception_justification "
                    "CHECK (length(btrim(justification)) > 0)"
                )
            )
            # The deparsed expression matches; its function binding does not.
            require_schema(physical_schema(db, schema), model_schema(db, schema))
            with pytest.raises(TransferError, match="unexpected_schema_dependencies"):
                validate_postgres(db, schema)
    finally:
        engine.dispose()
