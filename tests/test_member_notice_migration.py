"""Optional notice ownership preserves legacy messages through reversible migrations."""

from alembic.config import Config
from sqlalchemy import create_engine, inspect, text

from alembic import command
from church_roster_bot.core.config import settings


def test_member_notice_migration_preserves_legacy_outbox(migration_url, monkeypatch):
    monkeypatch.setattr(settings, "database_url", migration_url)
    config = Config("alembic.ini")
    engine = create_engine(migration_url)
    try:
        command.upgrade(config, "e31b4a6c8205")
        with engine.begin() as db:
            db.execute(
                text(
                    "INSERT INTO outbound_messages "
                    "(recipient_e164,payload,status,attempts,created_at) "
                    "VALUES ('+15550100001','{}','sent',1,CURRENT_TIMESTAMP)"
                )
            )
        command.upgrade(config, "f31a8b2c7406")
        with engine.connect() as db:
            assert db.scalar(text("SELECT volunteer_id FROM outbound_messages")) is None
            assert db.scalar(text("SELECT status FROM outbound_messages")) == "sent"
        command.downgrade(config, "e31b4a6c8205")
        assert "volunteer_id" not in {
            c["name"] for c in inspect(engine).get_columns("outbound_messages")
        }
        command.upgrade(config, "head")
        command.check(config)
    finally:
        engine.dispose()
