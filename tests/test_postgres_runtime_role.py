"""A disposable runtime role needs DML, not database or migration ownership."""

import secrets
from uuid import uuid4

import pytest
from psycopg import sql
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import sessionmaker

from alembic import command
from church_roster_bot.core.config import Settings
from church_roster_bot.core.database import database_engine
from church_roster_bot.database.errors import TransferError
from church_roster_bot.database.schema import migration_config
from church_roster_bot.database.transfer import transfer_sqlite
from church_roster_bot.members.schemas import VolunteerCreate
from church_roster_bot.scheduling.engine import RosterEngine
from tests.transfer_data import historical_sqlite


@pytest.fixture
def runtime_role(migration_url, tmp_path):
    url = make_url(migration_url)
    admin = create_engine(url)
    role = "phase42_runtime_" + uuid4().hex
    password = secrets.token_hex(32)
    role_oid = None
    runtime = None
    try:
        with admin.begin() as db:
            config = migration_config()
            config.attributes["connection"] = db
            command.upgrade(config, "head")
            schema = db.scalar(text("SELECT current_schema()"))
        source = historical_sqlite(tmp_path / "fictitious.sqlite3")
        plan = transfer_sqlite(source, url, schema=schema)
        transfer_sqlite(
            source,
            url,
            schema=schema,
            mode="apply",
            offline_confirmed=True,
            confirmation=plan["plan_token"],
        )
        with admin.begin() as db:
            raw = db.connection.driver_connection
            # The generated test password is never part of SQLAlchemy's SQL/error logging.
            with raw.cursor() as cursor:
                cursor.execute(
                    sql.SQL(
                        "CREATE ROLE {} LOGIN PASSWORD {} NOSUPERUSER NOCREATEDB NOCREATEROLE "
                        "NOREPLICATION NOBYPASSRLS"
                    ).format(sql.Identifier(role), sql.Literal(password))
                )
            role_oid = db.scalar(
                text("SELECT oid FROM pg_roles WHERE rolname=:name"), {"name": role}
            )
            for statement in (
                sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
                    sql.Identifier(url.database), sql.Identifier(role)
                ),
                sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(
                    sql.Identifier(schema), sql.Identifier(role)
                ),
                sql.SQL(
                    "GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA {} TO {}"
                ).format(sql.Identifier(schema), sql.Identifier(role)),
                sql.SQL("GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA {} TO {}").format(
                    sql.Identifier(schema), sql.Identifier(role)
                ),
            ):
                db.exec_driver_sql(statement.as_string(raw))
        runtime_url = url.set(username=role, password=password)
        runtime = database_engine(
            Settings(
                _env_file=None,
                app_env="test",
                database_url=runtime_url.render_as_string(hide_password=False),
            )
        )
        yield runtime, runtime_url, admin, source, schema, role
    finally:
        if runtime is not None:
            runtime.dispose()
        if role_oid is not None:
            with admin.begin() as db:
                assert (
                    db.scalar(text("SELECT oid FROM pg_roles WHERE rolname=:name"), {"name": role})
                    == role_oid
                )
                assert role.startswith("phase42_runtime_") and role != url.username
                raw = db.connection.driver_connection
                for statement in (
                    sql.SQL("REVOKE ALL ON ALL TABLES IN SCHEMA {} FROM {}").format(
                        sql.Identifier(schema), sql.Identifier(role)
                    ),
                    sql.SQL("REVOKE ALL ON ALL SEQUENCES IN SCHEMA {} FROM {}").format(
                        sql.Identifier(schema), sql.Identifier(role)
                    ),
                    sql.SQL("REVOKE ALL ON SCHEMA {} FROM {}").format(
                        sql.Identifier(schema), sql.Identifier(role)
                    ),
                    sql.SQL("REVOKE ALL ON DATABASE {} FROM {}").format(
                        sql.Identifier(url.database), sql.Identifier(role)
                    ),
                    sql.SQL("DROP ROLE {}").format(sql.Identifier(role)),
                ):
                    db.exec_driver_sql(statement.as_string(raw))
        admin.dispose()


pytestmark = [
    pytest.mark.postgres,
    pytest.mark.parametrize("migration_url", ["postgres"], indirect=True),
]


def test_runtime_dml_succeeds_but_ddl_and_import_are_denied(runtime_role):
    runtime, url, _, source, schema, role = runtime_role
    roster = RosterEngine(sessionmaker(runtime, expire_on_commit=False))
    assert (
        roster.register_volunteer(
            17,
            VolunteerCreate(
                name="Pessoa fictícia com papel mínimo", phone_e164="+15550100004", role_ids={17}
            ),
        )
        == 20
    )
    with runtime.connect() as db:
        flags = db.execute(
            text(
                "SELECT rolsuper,rolcreatedb,rolcreaterole,rolreplication,rolbypassrls "
                "FROM pg_roles WHERE rolname=:role"
            ),
            {"role": role},
        ).one()
        assert not any(flags)
    with pytest.raises(DBAPIError), runtime.begin() as db:
        db.exec_driver_sql("CREATE TABLE forbidden_runtime_ddl (id integer)")
    with pytest.raises(DBAPIError), runtime.begin() as db:
        db.exec_driver_sql("SET session_replication_role=replica")
    with pytest.raises(TransferError, match="migration_role_must_own_sequence"):
        # A fresh empty-target dry-run is required to reach the ownership check.
        # Verify mode also checks sequences while retaining the nonempty database.
        transfer_sqlite(source, url, schema=schema, mode="verify")


def test_pre_ping_recovers_and_interrupted_transaction_does_not_partially_commit(runtime_role):
    runtime, _, admin, _, _, _ = runtime_role
    with runtime.connect() as db:
        stale_pid = db.scalar(text("SELECT pg_backend_pid()"))
    with admin.begin() as db:
        assert db.scalar(text("SELECT pg_terminate_backend(:pid)"), {"pid": stale_pid})
    with runtime.connect() as db:
        assert db.scalar(text("SELECT pg_backend_pid()")) != stale_pid
    with pytest.raises(DBAPIError), runtime.begin() as db:
        db.execute(text("INSERT INTO churches(id,name,timezone) VALUES (9000,'Fictícia','UTC')"))
        interrupted_pid = db.scalar(text("SELECT pg_backend_pid()"))
        with admin.begin() as control:
            assert control.scalar(
                text("SELECT pg_terminate_backend(:pid)"), {"pid": interrupted_pid}
            )
        db.scalar(text("SELECT 1"))
    with runtime.connect() as db:
        assert db.scalar(text("SELECT count(*) FROM churches WHERE id=9000")) == 0
        assert db.scalar(text("SELECT count(*) FROM churches WHERE id=17")) == 1
