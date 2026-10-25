"""UTC reminder delivery, configuration scope, consent and restart safety."""

from datetime import UTC, date, datetime, time, timedelta

import httpx
import pytest
from sqlalchemy import func, select

from church_roster_bot.core.config import Settings
from church_roster_bot.core.time import utc_now
from church_roster_bot.members.models import CoordinatorMinistry, Volunteer
from church_roster_bot.ministries.models import Church
from church_roster_bot.notifications.worker import Worker
from church_roster_bot.scheduling.errors import NotAuthorized, RosterError
from church_roster_bot.scheduling.lifecycle import schedule_for
from church_roster_bot.scheduling.models import Assignment, ReminderPolicy
from church_roster_bot.scheduling.reminders import configure_reminders, enqueue_reminders
from church_roster_bot.scripts.simulate import simulate
from church_roster_bot.services.models import Slot, WorshipService
from church_roster_bot.whatsapp.client import MetaSendError
from church_roster_bot.whatsapp.models import OutboundMessage
from tests.test_distribution import prepare
from tests.test_invitation_delivery import RecordingClient, drain
from tests.test_phase2 import COORD, FIRST, bodies

pytestmark = pytest.mark.parametrize(
    "roster", ["sqlite", pytest.param("postgres", marks=pytest.mark.postgres)], indirect=True
)
NOW = datetime(2027, 1, 1, 12, tzinfo=UTC)
CONFIG = Settings(
    _env_file=None,
    whatsapp_reminder_template="lembrete_ficticio",
    whatsapp_notification_template="aviso_ficticio",
)


@pytest.fixture(autouse=True)
def reminder_clock(monkeypatch):
    for module in ("notifications.reminders", "scheduling.reminders"):
        monkeypatch.setattr(
            f"church_roster_bot.{module}.utc_now", lambda now=None: utc_now(now or NOW)
        )


def configure(roster, *, enabled=True, **kwargs):
    _, factory, ids = roster
    with factory.begin() as db:
        configure_reminders(
            db, ids["ministry"], "coordinator", ids["coordinator"], enabled=enabled, **kwargs
        )


def setup_reminder(roster, *, enabled=True):
    engine, factory, _ = roster
    members, slots = prepare(roster, month=date(2027, 1, 1), days=(2,), members=1)
    assignment = engine.assign(members[0], slots[0])
    if enabled:
        configure(roster)
    return members[0], slots[0], assignment


def tick(roster, config=CONFIG, now=NOW):
    with roster[1].begin() as db:
        return enqueue_reminders(db, config, now=now)


def notice(roster):
    with roster[1]() as db:
        return db.scalar(
            select(OutboundMessage).where(OutboundMessage.reminder_assignment_id.is_not(None))
        )


def test_reminder_at_local_clock_is_utc_and_sent_once_across_restart(roster):
    _, factory, _ = roster
    setup_reminder(roster)
    assert tick(roster, now=NOW - timedelta(seconds=1)) == 0
    assert tick(roster) == 1 and tick(roster) == 0
    row = notice(roster)
    assert row.reminder_due_at.replace(tzinfo=UTC) == NOW
    client = drain(factory, CONFIG)
    Worker(factory, CONFIG, client).recover_sending()
    assert tick(roster) == 0
    drain(factory, CONFIG, client)
    assert sum(phone == FIRST for phone, _ in client.calls) == 1
    assert (notice(roster).status, notice(roster).attempts) == ("sent", 1)


def test_disabled_policy_has_no_effect_and_recovery_sends_only_before_service(roster):
    setup_reminder(roster, enabled=False)
    assert tick(roster) == 0
    configure(roster)
    assert tick(roster, now=NOW + timedelta(hours=4)) == 1
    assert tick(roster, now=NOW + timedelta(days=3)) == 0


def test_opt_out_blocks_reminder_and_iniciar_recovers_only_once(roster):
    _, factory, _ = roster
    member, _, _ = setup_reminder(roster)
    with factory.begin() as db:
        db.get(Volunteer, member).invite_opt_in = False
    tick(roster)
    assert notice(roster).status == "blocked_consent"
    client = drain(factory, CONFIG)
    assert not any(phone == FIRST for phone, _ in client.calls)
    simulate(FIRST, "INICIAR", factory=factory, config=CONFIG)
    drain(factory, CONFIG, client)
    assert notice(roster).status == "sent"
    assert (
        sum(phone == FIRST and "Lembrete" in str(payload) for phone, payload in client.calls) == 1
    )


def test_outside_window_uses_only_reminder_template_and_recent_chat_can_unblock(roster):
    _, factory, _ = roster
    setup_reminder(roster)
    config = CONFIG.model_copy(update={"whatsapp_reminder_template": ""})
    tick(roster, config)
    assert notice(roster).status == "blocked_window"
    simulate(FIRST, "INICIAR", factory=factory, config=config)
    drain(factory, config)
    assert notice(roster).status == "sent" and notice(roster).payload["type"] == "text"


def test_approved_reminder_template_has_one_body_parameter(roster):
    setup_reminder(roster)
    tick(roster)
    payload = notice(roster).payload
    assert payload["type"] == "template"
    assert payload["template"]["name"] == "lembrete_ficticio"
    assert len(payload["template"]["components"]) == 1
    assert "02/01/2027" in str(payload) and "19:00" in str(payload)


@pytest.mark.parametrize(
    "change", ["cancel", "replacement", "moved", "disabled", "optout", "phone", "schedule"]
)
def test_send_revalidates_current_assignment_service_policy_and_recipient(roster, change):
    _, factory, ids = roster
    member, slot, assignment = setup_reminder(roster)
    tick(roster)
    with factory.begin() as db:
        if change == "cancel":
            db.get(Assignment, assignment).status = "cancelled"
        if change == "replacement":
            db.get(Assignment, assignment).status = "replacement_requested"
        if change == "moved":
            db.get(WorshipService, db.get(Slot, slot).service_id).start_time = time(20)
        if change == "disabled":
            db.get(ReminderPolicy, ids["ministry"]).enabled = False
        if change == "optout":
            db.get(Volunteer, member).invite_opt_in = False
        if change == "phone":
            db.get(Volunteer, member).phone_e164 = "+15550100099"
        if change == "schedule":
            schedule_for(db, ids["ministry"], date(2027, 1, 1)).state = "cancelled"
    client = drain(factory, CONFIG)
    assert not any(phone == FIRST for phone, _ in client.calls)
    assert notice(roster).attempts == 0


@pytest.mark.parametrize(
    "status",
    [
        "pending_confirmation",
        "replacement_requested",
        "replaced",
        "cancelled",
        "completed",
        "absent",
        "declined",
    ],
)
def test_only_confirmed_participations_are_reminded(roster, status):
    _, factory, _ = roster
    _, _, assignment = setup_reminder(roster)
    with factory.begin() as db:
        db.get(Assignment, assignment).status = status
    assert tick(roster) == 0


@pytest.mark.parametrize(
    "failure,status",
    [(MetaSendError(400, "synthetic"), "failed"), (httpx.ReadTimeout("synthetic"), "unknown")],
)
def test_transport_failure_does_not_repeat_reminder_or_cancel_participation(
    roster, failure, status
):
    _, factory, _ = roster
    _, _, assignment = setup_reminder(roster)
    tick(roster)
    client = drain(factory, CONFIG, RecordingClient(failure))
    Worker(factory, CONFIG, client).recover_sending()
    drain(factory, CONFIG, client)
    assert notice(roster).status == status
    assert sum(phone == FIRST for phone, _ in client.calls) == 1
    with factory() as db:
        assert db.get(Assignment, assignment).status == "confirmed"


def test_interrupted_sending_is_unknown_without_resend(roster):
    _, factory, _ = roster
    setup_reminder(roster)
    tick(roster)
    with factory.begin() as db:
        row = db.get(OutboundMessage, notice(roster).id)
        row.status, row.attempts = "sending", 1
    client = RecordingClient()
    Worker(factory, CONFIG, client).recover_sending()
    drain(factory, CONFIG, client)
    assert notice(roster).status == "unknown" and not any(
        phone == FIRST for phone, _ in client.calls
    )


def test_revoked_coordinator_cannot_change_configuration(roster):
    _, factory, ids = roster
    with factory.begin() as db:
        db.delete(db.scalar(select(CoordinatorMinistry)))
    with pytest.raises(NotAuthorized):
        configure(roster)
    with factory() as db:
        assert db.get(ReminderPolicy, ids["ministry"]) is None


@pytest.mark.parametrize(
    "days,hour", [(-1, time(9)), (31, time(9)), (1, time(9, 0, 1)), (1, time(9, tzinfo=UTC))]
)
def test_invalid_configuration_is_rejected(roster, days, hour):
    with pytest.raises(RosterError):
        configure(roster, days=days, hour=hour)


def test_chat_confirmation_is_required_and_configuration_persists(roster):
    _, factory, ids = roster
    simulate(COORD, "Oi", factory=factory, config=CONFIG)
    simulate(COORD, "LEMBRETES 2 08:30", factory=factory, config=CONFIG)
    with factory() as db:
        assert db.get(ReminderPolicy, ids["ministry"]) is None
    assert "configurados" in bodies(simulate(COORD, "SIM", factory=factory, config=CONFIG))
    with factory() as db:
        policy = db.get(ReminderPolicy, ids["ministry"])
        assert (policy.enabled, policy.days_before, policy.local_time) == (True, 2, time(8, 30))


def test_invalid_dst_clock_notifies_coordinator_once_and_does_not_send(roster):
    _, factory, ids = roster
    _, slot, _ = setup_reminder(roster)
    configure(roster, days=1, hour=time(2, 30))
    with factory.begin() as db:
        db.get(Church, ids["church"]).timezone = "America/New_York"
        db.get(WorshipService, db.get(Slot, slot).service_id).service_date = date(2027, 3, 15)
        schedule_for(db, ids["ministry"], date(2027, 3, 1), create=True).state = "open"
    instant = datetime(2027, 3, 14, 12, tzinfo=UTC)
    assert tick(roster, now=instant) == tick(roster, now=instant) == 0
    with factory() as db:
        assert db.scalar(select(func.count(OutboundMessage.id))) == 1


def test_church_timezone_changes_the_due_utc_instant(roster):
    _, factory, ids = roster
    setup_reminder(roster)
    with factory.begin() as db:
        db.get(Church, ids["church"]).timezone = "Asia/Tokyo"
    assert tick(roster, now=datetime(2027, 1, 1, 0, tzinfo=UTC)) == 1
    assert notice(roster).reminder_due_at.replace(tzinfo=UTC) == datetime(2027, 1, 1, 0, tzinfo=UTC)


def test_clock_rollback_defers_claim_until_the_recorded_due_instant(roster, monkeypatch):
    _, factory, _ = roster
    setup_reminder(roster)
    tick(roster)
    monkeypatch.setattr(
        "church_roster_bot.notifications.reminders.utc_now",
        lambda now=None: utc_now(now or NOW - timedelta(minutes=1)),
    )
    client = drain(factory, CONFIG)
    assert notice(roster).status == "blocked_time" and not client.calls
    assert tick(roster) == 0
    monkeypatch.setattr(
        "church_roster_bot.notifications.reminders.utc_now", lambda now=None: utc_now(now or NOW)
    )
    drain(factory, CONFIG, client)
    assert notice(roster).status == "sent" and notice(roster).attempts == 1


def test_reminder_time_after_service_requires_review_instead_of_sending(roster):
    _, factory, _ = roster
    setup_reminder(roster)
    configure(roster, days=0, hour=time(23))
    assert tick(roster, now=NOW + timedelta(days=1)) == 0
    assert notice(roster) is None
    with factory() as db:
        assert db.scalar(select(func.count(OutboundMessage.id))) == 1
