"""Legacy approvals retain their original history and never gain an invented snapshot."""

from alembic.config import Config
from sqlalchemy import create_engine, inspect, text

from alembic import command
from church_roster_bot.core.config import settings


def test_exception_snapshot_migration_preserves_legacy_approval(migration_url, monkeypatch):
    monkeypatch.setattr(settings, "database_url", migration_url)
    config = Config("alembic.ini")
    engine = create_engine(migration_url)
    snapshot = text(
        "SELECT id,coordinator_id,volunteer_id,slot_id,justification,status "
        "FROM third_day_requests ORDER BY id"
    )
    try:
        command.upgrade(config, "331c6a8e4509")
        statements = [
            "INSERT INTO churches (id,name,timezone) VALUES (1,'Fictícia','UTC')",
            "INSERT INTO ministries (id,church_id,name) VALUES (1,1,'Fictício')",
            "INSERT INTO roles (id,ministry_id,name,active) VALUES (1,1,'Fictícia',true)",
            "INSERT INTO coordinators (id,church_id,name,phone_e164,active) "
            "VALUES (1,1,'Fictício','+15550100000',true)",
            "INSERT INTO volunteers (id,church_id,name,phone_e164,active,invite_opt_in) "
            "VALUES (1,1,'Fictício','+15550100001',true,true)",
            "INSERT INTO worship_services (id,ministry_id,service_date,start_time) "
            "VALUES (1,1,'2030-01-03','19:00:00')",
            "INSERT INTO slots (id,service_id,role_id,capacity) VALUES (1,1,1,1)",
            "INSERT INTO third_day_requests "
            "(id,coordinator_id,volunteer_id,slot_id,justification,status,created_at) "
            "VALUES (1,1,1,1,'Autorização histórica fictícia','pending',CURRENT_TIMESTAMP)",
        ]
        with engine.begin() as db:
            for sql in statements:
                db.execute(text(sql))
            before = db.execute(snapshot).all()
        command.upgrade(config, "head")
        with engine.begin() as db:
            assert db.execute(snapshot).all() == before
            assert db.scalar(text("SELECT service_fingerprint FROM third_day_requests")) is None
            # A populated new snapshot must also survive downgrade of other history.
            db.execute(
                text("UPDATE third_day_requests SET service_fingerprint=:value"),
                {"value": "f" * 64},
            )
        command.downgrade(config, "331c6a8e4509")
        assert "service_fingerprint" not in {
            col["name"] for col in inspect(engine).get_columns("third_day_requests")
        }
        with engine.connect() as db:
            assert db.execute(snapshot).all() == before
        command.upgrade(config, "head")
        command.check(config)
    finally:
        engine.dispose()
