"""The incremental outbox migration preserves existing delivery records."""

from alembic.config import Config
from sqlalchemy import create_engine, inspect, text

from alembic import command
from church_roster_bot.core.config import settings


def test_invitation_migration_preserves_legacy_messages_and_can_downgrade(tmp_path, monkeypatch):
    url = f"sqlite+pysqlite:///{tmp_path / 'migration.sqlite3'}"
    monkeypatch.setattr(settings, "database_url", url)
    config = Config("alembic.ini")
    command.upgrade(config, "88ca949752d6")
    engine = create_engine(url)
    try:
        with engine.begin() as db:
            db.execute(
                text(
                    "INSERT INTO outbound_messages "
                    "(recipient_e164, payload, status, attempts, created_at) "
                    "VALUES ('+15550100001', '{}', 'sent', 1, CURRENT_TIMESTAMP)"
                )
            )
        command.upgrade(config, "head")
        with engine.connect() as db:
            row = db.execute(
                text("SELECT status, attempts, invitation_id, dedupe_key FROM outbound_messages")
            ).one()
            assert tuple(row) == ("sent", 1, None, None)
        command.downgrade(config, "88ca949752d6")
        assert "invitation_id" not in {
            column["name"] for column in inspect(engine).get_columns("outbound_messages")
        }
        with engine.connect() as db:
            assert db.scalar(text("SELECT status FROM outbound_messages")) == "sent"
        command.upgrade(config, "head")
    finally:
        engine.dispose()
