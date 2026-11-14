"""Real PostgreSQL imports, guarded plans, data proof and transactional failures."""

import json
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest
from sqlalchemy import create_engine, event, text
from sqlalchemy.exc import OperationalError

from alembic import command
from church_roster_bot.core.database import Base
from church_roster_bot.database import transfer
from church_roster_bot.database.errors import TransferError
from church_roster_bot.database.schema import migration_config
from church_roster_bot.database.source import file_hash
from tests.transfer_data import historical_sqlite

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.parametrize("migration_url", ["postgres"], indirect=True),
]


@pytest.fixture
def prepared(tmp_path, migration_url):
    engine = create_engine(migration_url)
    with engine.begin() as db:
        config = migration_config()
        config.attributes["connection"] = db
        command.upgrade(config, "head")
        schema = db.scalar(text("SELECT current_schema()"))
    path = historical_sqlite(tmp_path / "fictitious.sqlite3")
    yield path, migration_url, schema, engine
    engine.dispose()


def run(prepared, **kwargs):
    path, url, schema, _ = prepared
    return transfer.transfer_sqlite(path, url, schema=schema, **kwargs)


def apply(prepared, plan=None):
    plan = plan or run(prepared)
    return run(prepared, mode="apply", confirmation=plan["plan_token"], offline_confirmed=True)


def test_dry_run_full_import_reconciliation_and_next_ids(prepared):
    path, _, _, engine = prepared
    original = file_hash(path), path.stat().st_mtime_ns
    first, second = run(prepared), run(prepared)
    assert first["plan_token"] == second["plan_token"]
    assert first["sequence_plan"] == second["sequence_plan"]
    assert len(first["source_tables"]) == 28
    assert all(p["count"] > 0 for p in first["source_tables"].values())
    assert not any(p["count"] for p in first["target_before"].values())
    result = apply(prepared, first)
    assert result["status"] == "committed"
    assert result["target_after"] == result["source_tables"]
    assert run(prepared, mode="verify")["status"] == "verified-existing"
    assert (file_hash(path), path.stat().st_mtime_ns) == original
    with engine.begin() as db:
        church = Base.metadata.tables["churches"]
        assert (
            db.scalar(
                church.insert().values(name="Outra fictícia", timezone="UTC").returning(church.c.id)
            )
            == 18
        )
        assert dict(
            db.execute(
                text("SELECT status,count(*) FROM automation_invitations GROUP BY status")
            ).all()
        ) == {"pending": 1, "accepted": 1, "declined": 1, "expired": 1, "cancelled": 1}
        assert dict(
            db.execute(text("SELECT status,count(*) FROM outbound_messages GROUP BY status")).all()
        ) == {
            "pending": 1,
            "sent": 1,
            "failed": 1,
            "unknown": 1,
            "blocked_consent": 1,
            "blocked_window": 1,
        }
        assert tuple(
            db.execute(
                text(
                    "SELECT i.assignment_id,a.status FROM automation_invitations i "
                    "JOIN assignments a ON a.id=i.assignment_id WHERE i.id=18"
                )
            ).one()
        ) == (18, "cancelled")
        assert db.scalar(text("SELECT status FROM outbound_messages WHERE id=17")) == "unknown"
        assert db.scalar(text("SELECT invite_opt_in FROM volunteers WHERE id=18")) is False
        assert "123456789012345678901234567890" in db.scalar(
            text("SELECT details::text FROM audit_events WHERE id=17")
        )


@pytest.mark.parametrize("guard", ["missing_offline", "wrong_token", "nonempty"])
def test_explicit_authorization_and_empty_target_are_mandatory(prepared, guard):
    plan = run(prepared)
    if guard == "nonempty":
        with prepared[3].begin() as db:
            db.execute(text("INSERT INTO churches(id,name,timezone) VALUES (1,'Fictícia','UTC')"))
    expected = {
        "missing_offline": "explicit_offline",
        "wrong_token": "plan_confirmation",
        "nonempty": "target_must_be_empty",
    }[guard]
    with pytest.raises(TransferError, match=expected):
        run(
            prepared,
            mode="apply",
            offline_confirmed=guard != "missing_offline",
            confirmation="wrong" if guard == "wrong_token" else plan["plan_token"],
        )


def test_changed_source_invalidates_confirmation(prepared):
    plan = run(prepared)
    engine = create_engine("sqlite:///" + prepared[0].as_posix())
    try:
        with engine.begin() as db:
            db.execute(text("UPDATE churches SET name='Mudança fictícia'"))
    finally:
        engine.dispose()
    with pytest.raises(TransferError, match="plan_confirmation_mismatch"):
        apply(prepared, plan)
    assert not any(p["count"] for p in run(prepared)["target_before"].values())


@pytest.mark.parametrize("alteration", ["deferred", "cascade"])
def test_target_fk_enforcement_must_match_current_models(prepared, alteration):
    with prepared[3].begin() as db:
        if alteration == "deferred":
            db.execute(
                text(
                    "ALTER TABLE slots ALTER CONSTRAINT slots_service_id_fkey "
                    "DEFERRABLE INITIALLY DEFERRED"
                )
            )
        else:
            db.execute(text("ALTER TABLE slots DROP CONSTRAINT slots_service_id_fkey"))
            db.execute(
                text(
                    "ALTER TABLE slots ADD CONSTRAINT slots_service_id_fkey "
                    "FOREIGN KEY(service_id) REFERENCES worship_services(id) "
                    "ON DELETE CASCADE"
                )
            )
    with pytest.raises(TransferError, match="constraints_not_enforced"):
        run(prepared)


@pytest.mark.parametrize("revision", ["88ca949752d6", "a27b6c1d4e90", "231e4c9a7308"])
def test_legacy_source_imports_only_from_private_upgrade_without_changing_original(
    prepared, tmp_path, revision
):
    from tests.test_transfer_source import source_file

    directory = tmp_path / "legacy"
    directory.mkdir()
    path = source_file(directory, revision)
    before = file_hash(path), path.stat().st_mtime_ns
    legacy = (path, *prepared[1:])
    plan = run(legacy, upgrade_copy=True)
    assert plan["original_revision"] == revision
    assert plan["upgraded_private_copy"]
    result = run(
        legacy,
        mode="apply",
        confirmation=plan["plan_token"],
        offline_confirmed=True,
        upgrade_copy=True,
    )
    assert result["target_after"] == result["source_tables"]
    assert (file_hash(path), path.stat().st_mtime_ns) == before


@pytest.mark.parametrize("point", ["second_insert", "second_sequence"])
def test_partial_rows_and_sequence_restarts_roll_back_together(prepared, monkeypatch, point):
    plan = run(prepared)
    factory = transfer._engine
    seen = 0

    def faulty(*args):
        engine = factory(*args)

        @event.listens_for(engine, "before_cursor_execute")
        def interrupt(db, cursor, statement, params, context, many):
            nonlocal seen
            prefix = "INSERT INTO" if point == "second_insert" else "ALTER SEQUENCE"
            if statement.startswith(prefix):
                seen += 1
                if seen == 2:
                    raise RuntimeError("fictitious interrupted transaction")

        return engine

    with monkeypatch.context() as patch:
        patch.setattr(transfer, "_engine", faulty)
        with pytest.raises(RuntimeError, match="fictitious interrupted"):
            apply(prepared, plan)
    assert seen == 2
    afterwards = run(prepared)
    assert afterwards["sequence_plan"] == plan["sequence_plan"]
    assert afterwards["target_before"] == plan["target_before"]
    assert apply(prepared, afterwards)["status"] == "committed"


def test_same_counts_with_changed_contents_do_not_pass_verification(prepared):
    apply(prepared)
    with prepared[3].begin() as db:
        db.execute(text("UPDATE churches SET name='Conteúdo diferente fictício'"))
    with pytest.raises(TransferError, match="reconciliation_failed"):
        run(prepared, mode="verify")


def test_unsafe_sequence_is_detected_without_consuming_an_id(prepared):
    apply(prepared)
    with prepared[3].begin() as db:
        db.execute(text("ALTER SEQUENCE churches_id_seq RESTART WITH 1"))
    with pytest.raises(TransferError, match="sequence_reconciliation_failed"):
        run(prepared, mode="verify")
    with prepared[3].connect() as db:
        assert tuple(
            db.execute(text("SELECT last_value,is_called FROM churches_id_seq")).one()
        ) == (1, False)


def test_two_importers_cannot_assign_the_same_target(prepared, monkeypatch):
    plan = run(prepared)
    owner_locked, release = Event(), Event()
    original_lock = transfer._lock

    def hold_lock(db, schema, identity):
        original_lock(db, schema, identity)
        owner_locked.set()
        if not release.wait(20):
            raise RuntimeError("test lock timed out")

    monkeypatch.setattr(transfer, "_lock", hold_lock)
    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(apply, prepared, plan)
        try:
            assert owner_locked.wait(15)
            with pytest.raises(TransferError, match="another_import_is_running"):
                apply(prepared, plan)
        finally:
            release.set()
        assert first.result(timeout=20)["status"] == "committed"
    assert run(prepared, mode="verify")["status"] == "verified-existing"


def test_cli_dry_run_and_apply_reports_are_exclusive_and_sanitized(prepared, tmp_path):
    path, url, schema, _ = prepared
    environment = {**os.environ, "FICTITIOUS_MIGRATION_TARGET": url}
    base = [
        sys.executable,
        "-m",
        "church_roster_bot.scripts.migrate_sqlite",
        "--source",
        str(path),
        "--target-env",
        "FICTITIOUS_MIGRATION_TARGET",
        "--schema",
        schema,
    ]
    report = tmp_path / "plan.json"

    def invoke(extra):
        return subprocess.run(
            base + extra, env=environment, text=True, capture_output=True, timeout=60
        )

    dry = invoke(["--report", str(report)])
    assert dry.returncode == 0, dry.stdout + dry.stderr
    data = json.loads(report.read_text())
    assert data["mode"] == "dry-run"
    unchanged = report.read_bytes()
    assert invoke(["--report", str(report)]).returncode == 2
    assert report.read_bytes() == unchanged
    result = invoke(
        [
            "--report",
            str(tmp_path / "applied.json"),
            "--apply",
            "--offline-confirmed",
            "--confirm",
            data["plan_token"],
        ]
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "committed" in result.stdout
    assert url not in dry.stdout + dry.stderr + report.read_text() + result.stdout + result.stderr


def test_real_connection_failure_during_import_is_atomic(prepared, monkeypatch):
    plan = run(prepared)
    factory = transfer._engine
    killed = False

    def disconnecting(*args):
        engine = factory(*args)

        @event.listens_for(engine, "after_cursor_execute")
        def terminate(db, cursor, statement, params, context, many):
            nonlocal killed
            if statement.startswith("INSERT INTO") and not killed:
                killed = True
                pid = db.scalar(text("SELECT pg_backend_pid()"))
                with prepared[3].begin() as admin:
                    assert admin.scalar(text("SELECT pg_terminate_backend(:pid)"), {"pid": pid})

        return engine

    with monkeypatch.context() as patch:
        patch.setattr(transfer, "_engine", disconnecting)
        with pytest.raises(OperationalError):
            apply(prepared, plan)
    after = run(prepared)
    assert killed and after["target_before"] == plan["target_before"]
    assert after["sequence_plan"] == plan["sequence_plan"]


def test_connection_lost_at_commit_is_reported_as_uncertain_without_retry(prepared, monkeypatch):
    plan = run(prepared)
    factory = transfer._engine
    commits = []

    def disconnect_at_commit(*args):
        engine = factory(*args)

        @event.listens_for(engine, "commit")
        def terminate(db):
            commits.append(True)
            pid = db.scalar(text("SELECT pg_backend_pid()"))
            with prepared[3].begin() as admin:
                assert admin.scalar(text("SELECT pg_terminate_backend(:pid)"), {"pid": pid})

        return engine

    with monkeypatch.context() as patch:
        patch.setattr(transfer, "_engine", disconnect_at_commit)
        with pytest.raises(TransferError, match="commit_outcome_unknown_do_not_retry"):
            apply(prepared, plan)
    after = run(prepared)
    assert len(commits) == 1
    assert after["target_before"] == plan["target_before"]
    assert after["sequence_plan"] == plan["sequence_plan"]
