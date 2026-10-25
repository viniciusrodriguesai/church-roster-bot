"""Normal invitations add schema without replacing legacy message records."""

from alembic.config import Config
from sqlalchemy import create_engine, inspect, text

from alembic import command
from church_roster_bot.core.config import settings


def test_automation_migration_preserves_messages_and_round_trips(migration_url, monkeypatch):
    monkeypatch.setattr(settings, "database_url", migration_url)
    config = Config("alembic.ini")
    engine = create_engine(migration_url)
    try:
        command.upgrade(config, "b31a0c4e6721")
        with engine.begin() as db:
            db.execute(
                text(
                    "INSERT INTO outbound_messages "
                    "(recipient_e164,payload,status,attempts,created_at,dedupe_key) "
                    "VALUES ('+15550100001','{}','sent',1,CURRENT_TIMESTAMP,'legacy-test')"
                )
            )
        command.upgrade(config, "c31a2e6b8904")
        with engine.connect() as db:
            assert tuple(
                db.execute(
                    text(
                        "SELECT status,attempts,dedupe_key,automation_invitation_id "
                        "FROM outbound_messages"
                    )
                ).one()
            ) == ("sent", 1, "legacy-test", None)
        command.downgrade(config, "b31a0c4e6721")
        assert "automation_invitations" not in inspect(engine).get_table_names()
        with engine.connect() as db:
            assert db.scalar(text("SELECT dedupe_key FROM outbound_messages")) == "legacy-test"
        command.upgrade(config, "head")
        command.check(config)
    finally:
        engine.dispose()
