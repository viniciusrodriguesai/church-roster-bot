"""Configured monthly opening respects civil time, lifecycle, access and restarts."""

from datetime import UTC, date, datetime, time, timedelta

import pytest
from sqlalchemy import func, select

from church_roster_bot.audit.models import AuditEvent
from church_roster_bot.core.config import Settings
from church_roster_bot.members.models import CoordinatorMinistry
from church_roster_bot.ministries.models import Church
from church_roster_bot.notifications.worker import Worker
from church_roster_bot.scheduling.automation import configure_opening, open_monthly_schedules
from church_roster_bot.scheduling.errors import NotAuthorized, RosterError
from church_roster_bot.scheduling.lifecycle import schedule_for
from church_roster_bot.scheduling.models import AutomationPolicy, MonthlySchedule
from church_roster_bot.scripts.simulate import simulate
from church_roster_bot.whatsapp.client import MetaSendError
from church_roster_bot.whatsapp.models import OutboundMessage
from tests.conftest import make_service
from tests.test_invitation_delivery import RecordingClient, drain
from tests.test_phase2 import COORD, bodies

pytestmark = pytest.mark.parametrize(
    "roster", ["sqlite", pytest.param("postgres", marks=pytest.mark.postgres)], indirect=True
)
MONTH = date(2027, 1, 1)
DUE = datetime(2026, 12, 25, 12, tzinfo=UTC)
CONFIG = Settings(_env_file=None, whatsapp_notification_template="abertura_ficticia")


def configure(roster, **kwargs):
    _, factory, ids = roster
    with factory.begin() as db:
        configure_opening(
            db,
            ids["ministry"],
            "coordinator",
            ids["coordinator"],
            enabled=kwargs.pop("enabled", True),
            **kwargs,
        )


def service(roster, day=MONTH):
    engine, factory, ids = roster
    return make_service(engine, factory, ids["ministry"], day, {ids["offer"]: 1})


def tick(roster, now=DUE):
    _, factory, _ = roster
    with factory.begin() as db:
        return open_monthly_schedules(db, CONFIG, now=now)


def test_opens_at_configured_local_time_and_never_repeats_after_restart(roster):
    _, factory, ids = roster
    service(roster)
    configure(roster, day=25, hour=time(9))
    assert tick(roster, DUE - timedelta(seconds=1)) == 0
    assert tick(roster) == 1
    assert tick(roster) == 0
    assert tick(roster, DUE + timedelta(hours=2)) == 0
    with factory() as db:
        assert schedule_for(db, ids["ministry"], MONTH).state == "open"
        assert (
            db.scalar(
                select(func.count(AuditEvent.id)).where(AuditEvent.action == "schedule_auto_opened")
            )
            == 1
        )
        assert db.scalar(select(func.count(OutboundMessage.id))) == 1


def test_recovers_overdue_current_month_only_when_future_services_exist(roster):
    _, factory, ids = roster
    service(roster, date(2027, 1, 20))
    configure(roster)
    assert tick(roster, datetime(2027, 1, 5, 12, tzinfo=UTC)) == 1
    with factory() as db:
        assert schedule_for(db, ids["ministry"], MONTH).state == "open"


def test_no_policy_and_disabled_policy_do_not_open_schedules(roster):
    _, factory, _ = roster
    service(roster)
    assert tick(roster) == 0
    configure(roster, enabled=False)
    assert tick(roster) == 0
    with factory() as db:
        assert db.scalar(select(func.count(MonthlySchedule.id))) == 0


def test_does_not_invent_services_or_open_an_empty_month(roster):
    _, factory, _ = roster
    configure(roster)
    assert tick(roster) == 0
    with factory() as db:
        assert db.scalar(select(func.count(MonthlySchedule.id))) == 0


@pytest.mark.parametrize("state", ["open", "under_review", "published", "closed", "cancelled"])
def test_automatic_opening_does_not_reopen_or_override_other_states(roster, state):
    _, factory, ids = roster
    service(roster)
    configure(roster)
    with factory.begin() as db:
        schedule_for(db, ids["ministry"], MONTH, create=True).state = state
    assert tick(roster) == 0
    with factory() as db:
        assert schedule_for(db, ids["ministry"], MONTH).state == state


def test_a_month_with_only_past_services_is_not_opened_late(roster):
    service(roster)
    configure(roster)
    assert tick(roster, datetime(2027, 1, 2, 12, tzinfo=UTC)) == 0


@pytest.mark.parametrize(
    "kwargs", [{"day": 0}, {"day": 29}, {"hour": time(9, 0, 1)}, {"hour": time(9, tzinfo=UTC)}]
)
def test_policy_validates_day_and_local_minute_precision(roster, kwargs):
    with pytest.raises(RosterError):
        configure(roster, **kwargs)


def test_disabling_preserves_the_configured_day_and_hour(roster):
    _, factory, ids = roster
    configure(roster, day=15, hour=time(12, 34))
    configure(roster, enabled=False)
    with factory() as db:
        policy = db.get(AutomationPolicy, ids["ministry"])
        assert not policy.opening_enabled
        assert (policy.opening_day, policy.opening_time) == (15, time(12, 34))


def test_policy_updates_require_current_ministry_permissions(roster):
    _, factory, _ = roster
    with factory.begin() as db:
        db.delete(db.scalar(select(CoordinatorMinistry)))
    with pytest.raises(NotAuthorized):
        configure(roster)


def test_church_timezone_can_make_opening_due_on_previous_utc_day(roster):
    _, factory, ids = roster
    with factory.begin() as db:
        db.get(Church, ids["church"]).timezone = "Pacific/Auckland"
    service(roster)
    configure(roster)
    due = datetime(2026, 12, 24, 20, tzinfo=UTC)
    assert tick(roster, due - timedelta(seconds=1)) == 0
    assert tick(roster, due) == 1


def test_invalid_dst_opening_time_alerts_once_and_can_be_corrected(roster):
    _, factory, ids = roster
    with factory.begin() as db:
        db.get(Church, ids["church"]).timezone = "America/New_York"
    service(roster, date(2027, 4, 1))
    configure(roster, day=14, hour=time(2, 30))
    now = datetime(2027, 3, 14, 8, tzinfo=UTC)
    assert tick(roster, now) == tick(roster, now) == 0
    with factory() as db:
        assert db.scalar(select(func.count(OutboundMessage.id))) == 1
    configure(roster, day=14, hour=time(3, 30))
    assert tick(roster, now) == 1


def test_worker_opens_without_inbound_messages_and_keeps_state_after_send_failure(roster):
    _, factory, ids = roster
    with factory.begin() as db:
        db.get(Church, ids["church"]).timezone = "UTC"
    month = (datetime.now(UTC).date().replace(day=1) + timedelta(days=32)).replace(day=1)
    service(roster, month)
    configure(roster, day=1, hour=time(0))
    assert Worker(factory, CONFIG, RecordingClient()).maintain()

    class FailedClient:
        def send(self, _phone, _payload):
            raise MetaSendError(400, "synthetic")

    drain(factory, CONFIG, FailedClient())
    assert not Worker(factory, CONFIG, RecordingClient()).maintain()
    with factory() as db:
        assert schedule_for(db, ids["ministry"], month).state == "open"
        assert db.scalar(select(func.count(OutboundMessage.id))) == 1
        assert db.scalar(select(OutboundMessage.status)) == "failed"


def test_chat_requires_confirmation_to_enable_and_disable_opening(roster):
    _, factory, ids = roster
    simulate(COORD, "Oi", factory=factory)
    replies = simulate(COORD, "ABERTURA 25 09:00", factory=factory)
    assert "mês anterior" in bodies(replies)
    with factory() as db:
        assert db.get(AutomationPolicy, ids["ministry"]) is None
    assert "configurada" in bodies(simulate(COORD, "SIM", factory=factory))
    simulate(COORD, "ABERTURA DESATIVAR", factory=factory)
    simulate(COORD, "SIM", factory=factory)
    with factory() as db:
        assert not db.get(AutomationPolicy, ids["ministry"]).opening_enabled
