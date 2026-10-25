"""Urgent vacancies reuse explicit acceptance, eligibility and the durable outbox."""

from datetime import UTC, date, datetime, time, timedelta

import pytest
from sqlalchemy import func, select

from church_roster_bot.core.config import Settings
from church_roster_bot.members.models import CoordinatorMinistry, Volunteer
from church_roster_bot.notifications.worker import Worker
from church_roster_bot.scheduling.emergencies import configure_emergencies, dispatch_emergencies
from church_roster_bot.scheduling.errors import NotAuthorized, RosterError
from church_roster_bot.scheduling.lifecycle import request_replacement, schedule_for
from church_roster_bot.scheduling.models import Assignment, AutomationInvitation, EmergencyPolicy
from church_roster_bot.scheduling.offers import expire_offers, respond_offer
from church_roster_bot.scheduling.replacements import dispatch_replacements
from church_roster_bot.scheduling.waitlist import dispatch_waitlist, join_waitlist
from church_roster_bot.scripts.simulate import simulate
from church_roster_bot.services.models import Slot
from church_roster_bot.whatsapp.models import OutboundMessage
from tests.conftest import make_service
from tests.test_distribution import prepare
from tests.test_invitation_delivery import drain
from tests.test_phase2 import COORD, FIRST, bodies

pytestmark = pytest.mark.parametrize(
    "roster", ["sqlite", pytest.param("postgres", marks=pytest.mark.postgres)], indirect=True
)
NOW = datetime(2027, 1, 2, 16, tzinfo=UTC)
CONFIG = Settings(
    _env_file=None,
    whatsapp_automation_template="urgente_ficticio",
    whatsapp_notification_template="aviso_ficticio",
)


def configure(roster, *, enabled=True, **kwargs):
    _, factory, ids = roster
    with factory.begin() as db:
        configure_emergencies(
            db, ids["ministry"], "coordinator", ids["coordinator"], enabled=enabled, **kwargs
        )


def setup_emergency(roster, *, enabled=True):
    members, slots = prepare(roster, month=date(2027, 1, 1), days=(2,), members=3)
    if enabled:
        configure(roster)
    return members, slots[0]


def tick(roster, now=NOW):
    engine, factory, _ = roster
    with factory.begin() as db:
        return dispatch_emergencies(db, engine, CONFIG, now=now)


def offer(roster):
    with roster[1]() as db:
        return db.scalar(
            select(AutomationInvitation).where(AutomationInvitation.status == "pending")
        )


def answer(roster, member, offer_id, accept=True):
    engine, factory, _ = roster
    with factory.begin() as db:
        return respond_offer(db, engine, member, offer_id, accept, CONFIG, now=NOW)


def test_opt_in_policy_invites_only_inside_horizon_and_never_assigns_before_accept(roster):
    _, factory, _ = roster
    members, _ = setup_emergency(roster, enabled=False)
    assert tick(roster) == 0
    configure(roster)
    assert tick(roster, NOW - timedelta(seconds=1)) == 0
    assert tick(roster) == 1 and tick(roster) == 0
    row = offer(roster)
    assert row.kind == "emergency" and row.volunteer_id == members[0]
    assert row.expires_at.replace(tzinfo=UTC) == NOW + timedelta(minutes=15)
    assert "Urgente" in row.reason
    client = drain(factory, CONFIG)
    assert any(phone == FIRST for phone, _ in client.calls)
    with factory() as db:
        assert db.scalar(select(func.count(Assignment.id))) == 0
    result = answer(roster, members[0], row.id)
    assert result.status == "accepted" and answer(roster, members[0], row.id) == result


def test_decline_and_short_expiry_progress_without_reinviting_previous_candidates(roster):
    _, factory, _ = roster
    members, _ = setup_emergency(roster)
    tick(roster)
    answer(roster, members[0], offer(roster).id, False)
    assert tick(roster) == 1 and offer(roster).volunteer_id == members[1]
    with factory.begin() as db:
        expire_offers(db, CONFIG, now=NOW + timedelta(minutes=16))
    assert tick(roster, NOW + timedelta(minutes=16)) == 1
    assert offer(roster).volunteer_id == members[2]


def test_no_eligible_candidate_escalates_once_without_bypassing_monthly_limit(roster):
    engine, factory, ids = roster
    members, _ = setup_emergency(roster)
    with factory.begin() as db:
        for member in members[1:]:
            db.get(Volunteer, member).invite_opt_in = False
    for day in (5, 8):
        _, slots = make_service(
            engine, factory, ids["ministry"], date(2027, 1, day), {ids["offer"]: 1}
        )
        engine.assign(members[0], slots[ids["offer"]])
    assert tick(roster) == tick(roster) == 0
    with factory() as db:
        assert (
            db.scalar(
                select(func.count(OutboundMessage.id)).where(
                    OutboundMessage.dedupe_key.like("emergency:%:no_candidate:%")
                )
            )
            == 1
        )
        assert db.scalar(select(func.count(AutomationInvitation.id))) == 0


def test_waitlist_keeps_fifo_priority_and_uses_short_urgent_expiry(roster):
    engine, factory, _ = roster
    members, slot = setup_emergency(roster)
    with factory.begin() as db:
        join_waitlist(db, engine, members[1], slot)
    with factory.begin() as db:
        assert dispatch_waitlist(db, engine, CONFIG, now=NOW) == 1
    assert tick(roster) == 0 and offer(roster).volunteer_id == members[1]
    assert offer(roster).expires_at.replace(tzinfo=UTC) == NOW + timedelta(minutes=15)


def test_waitlist_cannot_offer_a_position_already_offered_by_emergency(roster):
    engine, factory, _ = roster
    members, slot = setup_emergency(roster)
    tick(roster)
    with factory.begin() as db:
        join_waitlist(db, engine, members[1], slot)
        assert dispatch_waitlist(db, engine, CONFIG, now=NOW) == 0
    with factory() as db:
        assert db.scalar(select(func.count(AutomationInvitation.id))) == 1


def test_new_urgent_replacement_uses_short_expiry_without_releasing_original(roster):
    engine, factory, _ = roster
    members, slot = setup_emergency(roster)
    original = engine.assign(members[0], slot)
    with factory.begin() as db:
        request_replacement(db, members[0], original, now=NOW)
    with factory.begin() as db:
        assert dispatch_replacements(db, engine, CONFIG, now=NOW) == 1
    row = offer(roster)
    assert row.kind == "replacement" and row.expires_at.replace(tzinfo=UTC) == NOW + timedelta(
        minutes=15
    )
    assert tick(roster) == 0
    with factory() as db:
        assert db.get(Assignment, original).status == "replacement_requested"
    assert answer(roster, row.volunteer_id, row.id).status == "accepted"


def test_candidate_opt_out_after_offer_advances_to_another_eligible_member(roster):
    _, factory, _ = roster
    members, _ = setup_emergency(roster)
    tick(roster)
    first = offer(roster).id
    with factory.begin() as db:
        db.get(Volunteer, members[0]).invite_opt_in = False
    assert tick(roster) == 1 and offer(roster).volunteer_id == members[1]
    assert answer(roster, members[0], first).status == "cancelled"


def test_no_emergency_offer_after_service_begins(roster):
    setup_emergency(roster)
    assert tick(roster, NOW + timedelta(hours=6)) == 0


def test_restarting_worker_does_not_duplicate_existing_offer(roster):
    _, factory, _ = roster
    setup_emergency(roster)
    tick(roster)
    Worker(factory, CONFIG).recover_sending()
    assert tick(roster) == 0
    with factory() as db:
        assert db.scalar(select(func.count(AutomationInvitation.id))) == 1


def test_permission_revocation_blocks_configuration(roster):
    _, factory, ids = roster
    with factory.begin() as db:
        db.delete(db.scalar(select(CoordinatorMinistry)))
    with pytest.raises(NotAuthorized):
        configure(roster)
    with factory() as db:
        assert db.get(EmergencyPolicy, ids["ministry"]) is None


@pytest.mark.parametrize("hours,minutes", [(0, 15), (73, 15), (6, 0), (6, 121)])
def test_invalid_policy_is_rejected(roster, hours, minutes):
    with pytest.raises(RosterError):
        configure(roster, hours=hours, minutes=minutes)


def test_chat_requires_confirmation_to_enable_emergencies(roster):
    _, factory, ids = roster
    simulate(COORD, "Oi", factory=factory, config=CONFIG)
    simulate(COORD, "EMERGENCIAS 6 15", factory=factory, config=CONFIG)
    with factory() as db:
        assert db.get(EmergencyPolicy, ids["ministry"]) is None
    assert "configuradas" in bodies(simulate(COORD, "SIM", factory=factory, config=CONFIG))


def test_replacement_search_prioritizes_earlier_service_over_request_creation_order(roster):
    engine, factory, ids = roster
    members, later_slot = setup_emergency(roster)
    later = engine.assign(members[0], later_slot)
    earlier_service = engine.create_service(
        ids["ministry"], date(2027, 1, 2), time(18), {ids["offer"]: 1}
    )
    with factory() as db:
        earlier_slot = db.scalar(select(Slot.id).where(Slot.service_id == earlier_service))
    earlier = engine.assign(members[1], earlier_slot)
    with factory.begin() as db:
        request_replacement(db, members[0], later, now=NOW)
        request_replacement(db, members[1], earlier, now=NOW)
    with factory.begin() as db:
        assert dispatch_replacements(db, engine, CONFIG, now=NOW) == 2
    with factory() as db:
        first = db.scalar(select(AutomationInvitation).order_by(AutomationInvitation.id))
        assert first.slot_id == earlier_slot


@pytest.mark.parametrize("state", ["draft", "under_review", "closed", "cancelled"])
def test_emergencies_do_not_mutate_inactive_schedules(roster, state):
    _, factory, ids = roster
    setup_emergency(roster)
    with factory.begin() as db:
        schedule_for(db, ids["ministry"], date(2027, 1, 1)).state = state
    assert tick(roster) == 0
    with factory() as db:
        assert db.scalar(select(func.count(AutomationInvitation.id))) == 0
