"""Waitlist migration round trips preserve the existing outbox on both engines."""

from alembic.config import Config
from sqlalchemy import create_engine, inspect, text

from alembic import command
from church_roster_bot.core.config import settings


def test_waitlist_migration_preserves_messages(migration_url, monkeypatch):
    monkeypatch.setattr(settings, "database_url", migration_url)
    config = Config("alembic.ini")
    engine = create_engine(migration_url)
    try:
        command.upgrade(config, "d31a0f9e2468")
        with engine.begin() as db:
            db.execute(
                text(
                    "INSERT INTO outbound_messages "
                    "(recipient_e164,payload,status,attempts,created_at) "
                    "VALUES ('+15550100001','{}','sent',1,CURRENT_TIMESTAMP)"
                )
            )
        command.upgrade(config, "e31b4a6c8205")
        with engine.connect() as db:
            assert db.scalar(text("SELECT COUNT(*) FROM waitlist_entries")) == 0
            assert db.scalar(text("SELECT status FROM outbound_messages")) == "sent"
        command.downgrade(config, "d31a0f9e2468")
        assert "waitlist_entries" not in inspect(engine).get_table_names()
        command.upgrade(config, "head")
        command.check(config)
    finally:
        engine.dispose()
