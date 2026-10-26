"""Coordinator scope is additive and reversible without rewriting message history."""

import pytest
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

from alembic import command
from church_roster_bot.core.config import settings


def test_coordinator_notice_migration_preserves_legacy_messages(migration_url, monkeypatch):
    monkeypatch.setattr(settings, "database_url", migration_url)
    config = Config("alembic.ini")
    engine = create_engine(migration_url)
    snapshot = text(
        "SELECT id,recipient_e164,payload,status,attempts FROM outbound_messages ORDER BY id"
    )
    try:
        command.upgrade(config, "231e4c9a7308")
        with engine.begin() as db:
            for status in ("pending", "sent", "unknown"):
                db.execute(
                    text(
                        "INSERT INTO outbound_messages "
                        "(recipient_e164,payload,status,attempts,created_at) "
                        "VALUES ('+15550100000','{}',:status,1,CURRENT_TIMESTAMP)"
                    ),
                    {"status": status},
                )
        with engine.connect() as db:
            before = db.execute(snapshot).all()
        command.upgrade(config, "head")
        inspector = inspect(engine)
        foreign_keys = {
            tuple(fk["constrained_columns"]): fk["referred_table"]
            for fk in inspector.get_foreign_keys("outbound_messages")
        }
        assert foreign_keys[("coordinator_id",)] == "coordinators"
        assert foreign_keys[("notice_ministry_id",)] == "ministries"
        with engine.connect() as db:
            assert (
                db.execute(
                    text("SELECT coordinator_id,notice_ministry_id FROM outbound_messages")
                ).all()
                == [(None, None)] * 3
            )
            assert db.execute(snapshot).all() == before
        with engine.begin() as db:
            db.execute(text("INSERT INTO churches (id,name,timezone) VALUES (1,'Fictícia','UTC')"))
            db.execute(text("INSERT INTO ministries (id,church_id,name) VALUES (1,1,'Fictício')"))
            db.execute(
                text(
                    "INSERT INTO coordinators (id,church_id,name,phone_e164,active) "
                    "VALUES (1,1,'Fictício','+15550100000',true)"
                )
            )
            db.execute(text("UPDATE outbound_messages SET coordinator_id=1,notice_ministry_id=1"))
            if engine.dialect.name == "postgresql":
                for column in ("coordinator_id", "notice_ministry_id"):
                    with pytest.raises(IntegrityError), db.begin_nested():
                        # Column names are fixed literals above, never user input.
                        db.execute(text(f"UPDATE outbound_messages SET {column}=9999"))
        command.downgrade(config, "231e4c9a7308")
        assert not {"coordinator_id", "notice_ministry_id"} & {
            col["name"] for col in inspect(engine).get_columns("outbound_messages")
        }
        with engine.connect() as db:
            assert db.execute(snapshot).all() == before
        command.upgrade(config, "head")
        command.check(config)
    finally:
        engine.dispose()
