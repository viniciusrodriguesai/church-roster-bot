"""Emergency policies are additive, reversible and leave old messages unchanged."""

from alembic.config import Config
from sqlalchemy import create_engine, inspect, text

from alembic import command
from church_roster_bot.core.config import settings


def test_emergency_migration_preserves_legacy_data(migration_url, monkeypatch):
    monkeypatch.setattr(settings, "database_url", migration_url)
    config = Config("alembic.ini")
    engine = create_engine(migration_url)
    try:
        command.upgrade(config, "031c7a9d2458")
        with engine.begin() as db:
            db.execute(
                text(
                    "INSERT INTO outbound_messages "
                    "(recipient_e164,payload,status,attempts,created_at) "
                    "VALUES ('+15550100001','{}','sent',1,CURRENT_TIMESTAMP)"
                )
            )
        command.upgrade(config, "131d2b8a9607")
        with engine.connect() as db:
            assert db.scalar(text("SELECT COUNT(*) FROM emergency_policies")) == 0
            assert db.scalar(text("SELECT status FROM outbound_messages")) == "sent"
        command.downgrade(config, "031c7a9d2458")
        assert "emergency_policies" not in inspect(engine).get_table_names()
        command.upgrade(config, "head")
        command.check(config)
    finally:
        engine.dispose()
