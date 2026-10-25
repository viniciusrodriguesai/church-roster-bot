"""Two workers opening the same month produce one transition and one alert."""

import pytest
from sqlalchemy import func, select

from church_roster_bot.audit.models import AuditEvent
from church_roster_bot.scheduling.automation import configure_opening, open_monthly_schedules
from church_roster_bot.scheduling.models import AutomationPolicy
from church_roster_bot.whatsapp.models import OutboundMessage
from tests.test_automation_concurrency import run_race
from tests.test_monthly_opening import CONFIG, DUE, configure, service

pytestmark = pytest.mark.parametrize(
    "roster", ["sqlite_file", pytest.param("postgres", marks=pytest.mark.postgres)], indirect=True
)


def test_competing_openers_create_one_schedule_transition(roster):
    _, factory, _ = roster
    service(roster)
    configure(roster)
    results = run_race(factory, lambda db, _: open_monthly_schedules(db, CONFIG, now=DUE))
    assert sorted(results) == [0, 1]
    with factory() as db:
        assert (
            db.scalar(
                select(func.count(AuditEvent.id)).where(AuditEvent.action == "schedule_auto_opened")
            )
            == 1
        )
        assert db.scalar(select(func.count(OutboundMessage.id))) == 1


def test_competing_policy_creations_keep_one_policy_row(roster):
    _, factory, ids = roster
    run_race(
        factory,
        lambda db, _: configure_opening(
            db,
            ids["ministry"],
            "coordinator",
            ids["coordinator"],
            enabled=True,
        ),
    )
    with factory() as db:
        assert db.scalar(select(func.count(AutomationPolicy.ministry_id))) == 1
        assert db.get(AutomationPolicy, ids["ministry"]).opening_enabled
