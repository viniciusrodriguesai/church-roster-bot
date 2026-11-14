"""Physical schema checks: no guessing revisions, silently missing FKs or weak checks."""

import re
from functools import lru_cache
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import (
    CheckConstraint,
    Column,
    MetaData,
    String,
    Table,
    UniqueConstraint,
    create_engine,
    inspect,
    select,
    text,
)

from alembic import command
from church_roster_bot.core import models  # noqa: F401
from church_roster_bot.core.database import Base
from church_roster_bot.database.errors import TransferError


def migration_config() -> Config:
    path = Path(__file__).resolve().parents[3] / "alembic.ini"
    if not path.is_file():
        raise TransferError("migration_scripts_required")
    config = Config(str(path))
    config.attributes["configure_logger"] = False
    return config


def head_revision() -> str:
    return ScriptDirectory.from_config(migration_config()).get_current_head()


def checked_revision(value: str) -> str:
    scripts = ScriptDirectory.from_config(migration_config())
    supported = {r.revision for r in scripts.iterate_revisions("head", "88ca949752d6")}
    supported.add("88ca949752d6")
    if value not in supported:
        raise TransferError("unsupported_source_revision")
    return value


def sql_signature(value: str) -> str:
    # Current project CHECKs use IN, BETWEEN, comparisons and trim, never arbitrary
    # executable SQL. Normalize PostgreSQL's equivalent deparser output only.
    pieces = re.split(r"('(?:[^']|'')*')", value)
    for i in range(0, len(pieces), 2):
        pieces[i] = pieces[i].lower().replace('"', "")
        # Current checks only combine predicates with AND. Removing redundant
        # deparser parentheses is safe for those, but would erase the meaning
        # of AND/OR grouping. Fail closed for unsupported boolean expressions.
        if re.search(r"\b(?:or|not)\b", pieces[i]):
            raise TransferError("unsupported_schema_expression")
        pieces[i] = re.sub(r"::(?:character varying|text|integer|bigint)(?:\[\])?", "", pieces[i])
    value = "".join(pieces)
    value = re.sub(r"=\s*any\s*\(\s*array\s*\[([^]]*)\]\s*\)", r" in (\1)", value)
    value = re.sub(
        r"(\w+)\s+between\s+(\d+)\s+and\s+(\d+)",
        r"\1>=\2 and \1<=\3",
        value,
    )
    value = value.replace("btrim(", "trim(")
    parts = re.split(r"('(?:[^']|'')*')", value)
    for i in range(0, len(parts), 2):
        parts[i] = re.sub(r"[\s()]", "", parts[i]).replace("trimbothfrom", "trim")
    value = "".join(parts)
    return re.sub(r"=anyarray\[([^]]*)\]", r"in\1", value)


def _predicate(index, dialect):
    value = index.dialect_options[dialect].get("where")
    return sql_signature(str(value) if value is not None else "")


def physical_schema(connection, schema=None) -> dict:
    inspector = inspect(connection)
    if inspector.get_view_names(schema=schema):
        raise TransferError("unexpected_views")
    if connection.dialect.name == "postgresql" and (
        inspector.get_materialized_view_names(schema=schema)
        or inspector.get_foreign_table_names(schema=schema)
    ):
        raise TransferError("unexpected_relations")
    result = {}
    for name in sorted(inspector.get_table_names(schema=schema)):
        if name in {"alembic_version", "sqlite_sequence"}:
            continue
        columns = inspector.get_columns(name, schema=schema)
        fk_options = (
            {"postgresql_ignore_search_path": True}
            if connection.dialect.name == "postgresql"
            else {}
        )
        result[name] = {
            "columns": [
                (
                    c["name"],
                    str(c["type"].compile(dialect=connection.dialect)).lower(),
                    c["nullable"],
                )
                for c in columns
            ],
            "pk": tuple(inspector.get_pk_constraint(name, schema=schema)["constrained_columns"]),
            "fk": sorted(
                (
                    tuple(f["constrained_columns"]),
                    f.get("referred_schema") or schema,
                    f["referred_table"],
                    tuple(f["referred_columns"]),
                )
                for f in inspector.get_foreign_keys(name, schema=schema, **fk_options)
            ),
            "unique": sorted(
                tuple(u["column_names"])
                for u in inspector.get_unique_constraints(name, schema=schema)
            ),
            "checks": sorted(
                sql_signature(c["sqltext"])
                for c in inspector.get_check_constraints(name, schema=schema)
            ),
            "indexes": sorted(
                (
                    tuple(idx["column_names"]),
                    bool(idx["unique"]),
                    sql_signature(
                        str(
                            idx.get("dialect_options", {}).get(
                                connection.dialect.name + "_where", ""
                            )
                        )
                    ),
                )
                for idx in inspector.get_indexes(name, schema=schema)
                if not idx.get("duplicates_constraint")
            ),
        }
    return result


def model_schema(connection, schema=None) -> dict:
    result = {}
    for table in Base.metadata.sorted_tables:
        result[table.name] = {
            "columns": [
                (c.name, str(c.type.compile(dialect=connection.dialect)).lower(), c.nullable)
                for c in table.columns
            ],
            "pk": tuple(c.name for c in table.primary_key.columns),
            "fk": sorted(
                (
                    tuple(e.parent.name for e in fk.elements),
                    schema,
                    fk.referred_table.name,
                    tuple(e.column.name for e in fk.elements),
                )
                for fk in table.foreign_key_constraints
            ),
            "unique": sorted(
                tuple(c.name for c in u.columns)
                for u in table.constraints
                if isinstance(u, UniqueConstraint)
            ),
            "checks": sorted(
                sql_signature(str(c.sqltext))
                for c in table.constraints
                if isinstance(c, CheckConstraint)
            ),
            "indexes": sorted(
                (
                    tuple(c.name for c in idx.columns),
                    bool(idx.unique),
                    _predicate(idx, connection.dialect.name),
                )
                for idx in table.indexes
            ),
        }
    return result


def require_schema(actual: dict, expected: dict) -> None:
    if actual.keys() != expected.keys():
        raise TransferError("unexpected_or_missing_tables")
    for name in sorted(expected):
        # Physical column ordering may legitimately differ after Alembic's additive
        # migrations. All reads/inserts address explicit column names.
        left, right = dict(actual[name]), dict(expected[name])
        left["columns"], right["columns"] = sorted(left["columns"]), sorted(right["columns"])
        if left != right:
            raise TransferError("schema_drift", table=name)


@lru_cache(maxsize=16)
def revision_schema(revision: str) -> dict:
    checked_revision(revision)
    engine = create_engine("sqlite://")
    try:
        with engine.begin() as connection:
            config = migration_config()
            config.attributes["connection"] = connection
            command.upgrade(config, revision)
            return physical_schema(connection)
    finally:
        engine.dispose()


def _validate_column_generators(connection, schema: str) -> None:
    generators = connection.execute(
        text(
            "SELECT r.relname AS table_name, a.attname AS column_name, "
            "a.attidentity AS identity_kind, a.attgenerated AS generated_kind, "
            "pg_get_expr(d.adbin,d.adrelid) AS default_sql, "
            "pg_get_serial_sequence(format('%I.%I',n.nspname,r.relname),a.attname) "
            "AS serial_sequence, "
            "format('nextval(%L::regclass)', "
            "pg_get_serial_sequence(format('%I.%I',n.nspname,r.relname),a.attname)"
            "::regclass::text) AS serial_default_sql "
            "FROM pg_attribute a JOIN pg_class r ON r.oid=a.attrelid "
            "JOIN pg_namespace n ON n.oid=r.relnamespace "
            "LEFT JOIN pg_attrdef d ON d.adrelid=a.attrelid AND d.adnum=a.attnum "
            "WHERE n.nspname=:schema AND r.relkind IN ('r','p') "
            "AND a.attnum>0 AND NOT a.attisdropped"
        ),
        {"schema": schema},
    ).mappings()
    for generator in generators:
        if generator["table_name"] not in Base.metadata.tables:
            continue
        table = Base.metadata.tables[generator["table_name"]]
        column = table.c[generator["column_name"]]
        # Current models only generate values through SERIAL primary keys.
        # Ownership alone does not prove that the column still uses its sequence.
        if (
            generator["identity_kind"]
            or generator["generated_kind"]
            or (
                (
                    not generator["serial_sequence"]
                    or generator["default_sql"] != generator["serial_default_sql"]
                )
                if column is table.autoincrement_column
                else generator["default_sql"] is not None
            )
        ):
            raise TransferError("unexpected_column_generator", table=table.name, column=column.name)


def validate_postgres(connection, schema: str) -> None:
    if connection.dialect.name != "postgresql":
        raise TransferError("postgresql_target_required")
    if connection.scalar(
        text(
            "SELECT count(*) FROM pg_class r JOIN pg_namespace n ON n.oid=r.relnamespace "
            "WHERE n.nspname=:schema AND (r.relrowsecurity OR r.relforcerowsecurity)"
        ),
        {"schema": schema},
    ):
        raise TransferError("unexpected_row_security")
    inspector = inspect(connection)
    if "alembic_version" not in inspector.get_table_names(schema=schema):
        raise TransferError("target_revision_required")
    version_table = Table(
        "alembic_version", MetaData(), Column("version_num", String(32)), schema=schema
    )
    versions = connection.scalars(select(version_table.c.version_num)).all()
    if versions != [head_revision()]:
        raise TransferError("target_revision_mismatch")
    require_schema(physical_schema(connection, schema), model_schema(connection, schema))
    _validate_column_generators(connection, schema)
    bad = connection.scalar(
        text(
            "SELECT count(*) FROM pg_constraint c JOIN pg_namespace n ON n.oid=c.connamespace "
            "WHERE n.nspname=:schema AND (NOT c.convalidated OR c.condeferrable "
            "OR (c.contype='f' AND (c.confdeltype<>'a' OR c.confupdtype<>'a')))"
        ),
        {"schema": schema},
    )
    triggers = connection.scalar(
        text(
            "SELECT count(*) FROM pg_trigger t JOIN pg_class r ON r.oid=t.tgrelid "
            "JOIN pg_namespace n ON n.oid=r.relnamespace WHERE n.nspname=:schema "
            "AND (NOT t.tgisinternal OR t.tgenabled NOT IN ('O','A'))"
        ),
        {"schema": schema},
    )
    invalid_indexes = connection.scalar(
        text(
            "SELECT count(*) FROM pg_index i JOIN pg_class r ON r.oid=i.indrelid "
            "JOIN pg_namespace n ON n.oid=r.relnamespace WHERE n.nspname=:schema "
            "AND (NOT i.indisvalid OR NOT i.indisready OR NOT i.indislive)"
        ),
        {"schema": schema},
    )
    # Current metadata has no deferred constraints or cascading FK actions.
    # Reflection alone omits these enforcement details and invalid index state.
    if (
        bad
        or triggers
        or invalid_indexes
        or connection.scalar(text("SHOW session_replication_role")) != "origin"
    ):
        raise TransferError("constraints_not_enforced")
