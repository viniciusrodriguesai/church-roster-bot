"""Official PG17 clients restore fictional data into a newly created database."""

import hashlib
import json
import os
import shutil
import subprocess
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

from alembic import command
from church_roster_bot.database.schema import migration_config
from church_roster_bot.database.transfer import transfer_sqlite
from church_roster_bot.members.schemas import VolunteerCreate
from church_roster_bot.scheduling.engine import RosterEngine
from tests.transfer_data import historical_sqlite


def client(tool, args, url, *, output=None, input_file=None):
    """Keep credentials out of argv; never discover or use another container."""
    native = Path("/usr/lib/postgresql/17/bin") / tool
    environment = os.environ.copy()
    if native.is_file():
        executable = [
            str(native),
            "--dbname",
            url.set(drivername="postgresql", password=None).render_as_string(hide_password=False),
        ]
        environment["PGPASSWORD"] = url.password or ""
        environment["PGCONNECT_TIMEOUT"] = "5"
    else:
        container = os.getenv("POSTGRES_TEST_CONTAINER")
        if not container or not shutil.which("docker"):
            pytest.fail("PG17 clients or explicit POSTGRES_TEST_CONTAINER and Docker required")
        executable = [
            "docker",
            "exec",
            "-i",
            container,
            tool,
            "--username",
            url.username,
            "--dbname",
            url.database,
        ]
    result = subprocess.run(
        executable + args,
        env=environment,
        stdout=output or subprocess.PIPE,
        stdin=input_file,
        stderr=subprocess.PIPE,
        timeout=90,
    )
    assert result.returncode == 0, result.stderr.decode(errors="replace")
    return result.stdout


@pytest.mark.postgres
@pytest.mark.parametrize("migration_url", ["postgres"], indirect=True)
def test_pg17_dump_restore_preserves_rows_sequences_and_application(migration_url, tmp_path):
    url = make_url(migration_url)
    admin = create_engine(url, isolation_level="AUTOCOMMIT")
    source_engine = create_engine(url)
    restored_engine = None
    database = "phase42_restore_" + uuid4().hex
    created_oid = None
    try:
        with source_engine.begin() as db:
            config = migration_config()
            config.attributes["connection"] = db
            command.upgrade(config, "head")
            schema = db.scalar(text("SELECT current_schema()"))
        source = historical_sqlite(tmp_path / "fictitious.sqlite3")
        plan = transfer_sqlite(source, url, schema=schema)
        imported = transfer_sqlite(
            source,
            url,
            schema=schema,
            mode="apply",
            confirmation=plan["plan_token"],
            offline_confirmed=True,
        )
        evidence = Path(os.environ.get("ROSTER_EVIDENCE_DIR", str(tmp_path)))
        archive = evidence / "fictitious-pg17-validation.dump"
        with archive.open("xb") as output:
            client(
                "pg_dump",
                [
                    "--format=custom",
                    "--strict-names",
                    "--schema",
                    schema,
                    "--no-owner",
                    "--no-privileges",
                ],
                url,
                output=output,
            )
        assert archive.stat().st_size > 0
        # pg_restore --list has no --dbname; inspect the archive locally instead.
        native = Path("/usr/lib/postgresql/17/bin/pg_restore")
        if native.is_file():
            listing = subprocess.run(
                [str(native), "--list", str(archive)], capture_output=True, timeout=30, check=True
            ).stdout
        else:
            container = os.environ["POSTGRES_TEST_CONTAINER"]
            with archive.open("rb") as data:
                listing = subprocess.run(
                    ["docker", "exec", "-i", container, "pg_restore", "--list"],
                    stdin=data,
                    capture_output=True,
                    timeout=30,
                    check=True,
                ).stdout
        assert b"TABLE DATA" in listing and b"SEQUENCE SET" in listing
        with admin.connect() as db:
            db.exec_driver_sql(f'CREATE DATABASE "{database}"')
            created_oid = db.scalar(
                text("SELECT oid FROM pg_database WHERE datname=:name"), {"name": database}
            )
        target = url.set(database=database)
        with archive.open("rb") as data:
            client(
                "pg_restore",
                ["--single-transaction", "--exit-on-error", "--no-owner", "--no-privileges"],
                target,
                input_file=data,
            )
        verified = transfer_sqlite(source, target, schema=schema, mode="verify")
        assert verified["target_after"] == imported["source_tables"]
        restored_engine = create_engine(target)
        with restored_engine.begin() as db:
            config = migration_config()
            config.attributes["connection"] = db
            command.upgrade(config, "head")
            command.check(config)
        roster = RosterEngine(sessionmaker(restored_engine, expire_on_commit=False))
        identity = roster.register_volunteer(
            17,
            VolunteerCreate(
                name="Pessoa fictícia após restauração", phone_e164="+15550100004", role_ids={17}
            ),
        )
        assert identity == 20
        report = {
            "status": "passed",
            "archive_sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
            "archive_bytes": archive.stat().st_size,
            "schema": schema,
            "restored_database": database,
            "tables_before": imported["source_tables"],
            "tables_after": verified["target_after"],
            "sequences": verified["sequence_plan"],
            "application_new_volunteer_id": identity,
            "alembic_upgrade_check": "passed",
        }
        (evidence / "backup-restoration.json").write_text(json.dumps(report, indent=2))
    finally:
        if restored_engine is not None:
            restored_engine.dispose()
        source_engine.dispose()
        if created_oid is not None:
            # Cleanup can only address the exact database created by this test.
            with admin.connect() as db:
                assert (
                    db.scalar(
                        text("SELECT oid FROM pg_database WHERE datname=:name"), {"name": database}
                    )
                    == created_oid
                )
                assert database.startswith("phase42_restore_") and database != url.database
                db.exec_driver_sql(f'DROP DATABASE "{database}"')
        admin.dispose()
