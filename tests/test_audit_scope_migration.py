"""Audit backfill infers only proven ownership and retains unscoped legacy events."""

from alembic.config import Config
from sqlalchemy import create_engine, inspect, text

from alembic import command
from church_roster_bot.core.config import settings


def test_audit_scope_migration_preserves_and_scopes_legacy_history(migration_url, monkeypatch):
    monkeypatch.setattr(settings, "database_url", migration_url)
    config = Config("alembic.ini")
    engine = create_engine(migration_url)
    try:
        command.upgrade(config, "131d2b8a9607")
        with engine.begin() as db:
            db.execute(
                text(
                    "INSERT INTO churches (id,name,timezone) "
                    "VALUES (1,'Fictícia','America/Sao_Paulo')"
                )
            )
            db.execute(text("INSERT INTO ministries (id,church_id,name) VALUES (1,1,'Diaconato')"))
            for kind in ("ministry", "volunteer"):
                db.execute(
                    text(
                        "INSERT INTO audit_events "
                        "(church_id,actor_type,actor_id,action,entity_type,entity_id,"
                        "details,created_at) "
                        "VALUES (1,'system',0,'legacy',:kind,1,'{}',CURRENT_TIMESTAMP)"
                    ),
                    {"kind": kind},
                )
        command.upgrade(config, "231e4c9a7308")
        with engine.connect() as db:
            assert (
                db.scalar(text("SELECT ministry_id FROM audit_events WHERE entity_type='ministry'"))
                == 1
            )
            assert (
                db.scalar(
                    text("SELECT ministry_id FROM audit_events WHERE entity_type='volunteer'")
                )
                is None
            )
            assert db.scalar(text("SELECT COUNT(*) FROM audit_events")) == 2
        command.downgrade(config, "131d2b8a9607")
        with engine.connect() as db:
            assert db.scalar(text("SELECT COUNT(*) FROM audit_events")) == 2
        assert "ministry_id" not in {c["name"] for c in inspect(engine).get_columns("audit_events")}
        command.upgrade(config, "head")
        command.check(config)
    finally:
        engine.dispose()
