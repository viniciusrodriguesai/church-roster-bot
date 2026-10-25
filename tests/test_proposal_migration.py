"""The additive proposal migration is reversible on SQLite and isolated PostgreSQL schemas."""

from alembic.config import Config
from sqlalchemy import create_engine, inspect, text

from alembic import command
from church_roster_bot.core.config import settings


def test_proposal_migration_preserves_legacy_outbox(migration_url, monkeypatch):
    monkeypatch.setattr(settings, "database_url", migration_url)
    config = Config("alembic.ini")
    engine = create_engine(migration_url)
    try:
        command.upgrade(config, "a27b6c1d4e90")
        with engine.begin() as db:
            db.execute(
                text(
                    "INSERT INTO outbound_messages "
                    "(recipient_e164,payload,status,attempts,created_at) "
                    "VALUES ('+15550100001','{}','sent',1,CURRENT_TIMESTAMP)"
                )
            )
        command.upgrade(config, "b31a0c4e6721")
        assert {"roster_proposals", "roster_proposal_items"} <= set(
            inspect(engine).get_table_names()
        )
        with engine.connect() as db:
            assert tuple(
                db.execute(text("SELECT status,attempts FROM outbound_messages")).one()
            ) == ("sent", 1)
        command.downgrade(config, "a27b6c1d4e90")
        assert "roster_proposals" not in inspect(engine).get_table_names()
        with engine.connect() as db:
            assert db.scalar(text("SELECT COUNT(*) FROM outbound_messages")) == 1
        command.upgrade(config, "head")
        command.check(config)
    finally:
        engine.dispose()
