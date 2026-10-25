"""FIFO notifications preserve eligibility and require explicit acceptance."""

from datetime import UTC, date, datetime, time, timedelta

import pytest
from sqlalchemy import func, select

from church_roster_bot.core.config import Settings
from church_roster_bot.members.models import Volunteer
from church_roster_bot.scheduling.errors import (
    MonthlyLimit,
    NotAuthorized,
    RosterError,
    Unavailable,
)
from church_roster_bot.scheduling.lifecycle import schedule_for
from church_roster_bot.scheduling.models import Assignment, AutomationInvitation, WaitlistEntry
from church_roster_bot.scheduling.offers import expire_offers, respond_offer
from church_roster_bot.scheduling.waitlist import cancel_waitlist, dispatch_waitlist, join_waitlist
from church_roster_bot.scripts.simulate import simulate
from church_roster_bot.services.models import Slot, WorshipService
from tests.conftest import make_service
from tests.test_distribution import prepare
from tests.test_invitation_delivery import drain
from tests.test_phase2 import FIRST, SECOND, bodies

pytestmark = pytest.mark.parametrize(
    "roster", ["sqlite", pytest.param("postgres", marks=pytest.mark.postgres)], indirect=True
)
CONFIG = Settings(
    _env_file=None,
    whatsapp_automation_template="fila_ficticia",
    whatsapp_notification_template="aviso_ficticio",
)


def setup_queue(roster, *, full=True, capacity=1):
    engine, factory, ids = roster
    month = (date.today().replace(day=1) + timedelta(days=32)).replace(day=1)
    members, slots = prepare(roster, month=month, members=3)
    with factory.begin() as db:
        db.get(Slot, slots[0]).capacity = capacity
    original = engine.assign(members[2], slots[0]) if full else None
    return members, slots[0], original


def join(roster, member, slot):
    engine, factory, _ = roster
    with factory.begin() as db:
        return join_waitlist(db, engine, member, slot)


def dispatch(roster):
    engine, factory, _ = roster
    with factory.begin() as db:
        return dispatch_waitlist(db, engine, CONFIG)


def free_slot(roster, assignment_id):
    _, factory, _ = roster
    with factory.begin() as db:
        db.get(Assignment, assignment_id).status = "cancelled"


def respond(roster, member, offer, accept=True):
    engine, factory, _ = roster
    with factory.begin() as db:
        return respond_offer(db, engine, member, offer, accept, CONFIG)


def offer_for(roster, entry_id):
    with roster[1]() as db:
        return db.get(WaitlistEntry, entry_id).offer_id


def test_full_slot_waits_then_notifies_first_entry_without_assigning(roster):
    _, factory, _ = roster
    members, slot, original = setup_queue(roster)
    first = join(roster, members[0], slot)
    second = join(roster, members[1], slot)
    assert join(roster, members[0], slot) == first
    assert dispatch(roster) == 0
    free_slot(roster, original)
    assert dispatch(roster) == 1
    assert dispatch(roster) == 0
    assert offer_for(roster, first) and offer_for(roster, second) is None
    client = drain(factory, CONFIG)
    assert sum(phone == FIRST for phone, _ in client.calls) == 1
    with factory() as db:
        assert (
            db.scalar(select(func.count(Assignment.id)).where(Assignment.status == "confirmed"))
            == 0
        )
    decision = respond(roster, members[0], offer_for(roster, first))
    assert decision.status == "accepted"
    with factory() as db:
        assert db.get(WaitlistEntry, first).status == "accepted"


def test_decline_advances_to_next_entry_after_worker_restart(roster):
    members, slot, original = setup_queue(roster)
    first, second = [join(roster, m, slot) for m in members[:2]]
    free_slot(roster, original)
    dispatch(roster)
    assert respond(roster, members[0], offer_for(roster, first), False).status == "declined"
    assert dispatch(roster) == 1
    assert offer_for(roster, second)


def test_expiry_advances_queue_and_does_not_accept_late_responses(roster):
    _, factory, _ = roster
    members, slot, original = setup_queue(roster)
    first, second = [join(roster, m, slot) for m in members[:2]]
    free_slot(roster, original)
    dispatch(roster)
    first_offer = offer_for(roster, first)
    with factory.begin() as db:
        offer = db.get(AutomationInvitation, first_offer)
        offer.created_at = datetime.now(UTC) - timedelta(hours=2)
        offer.expires_at = datetime.now(UTC) - timedelta(hours=1)
    with factory.begin() as db:
        assert expire_offers(db, CONFIG) == 1
        assert db.get(WaitlistEntry, first).status == "expired"
    assert dispatch(roster) == 1 and offer_for(roster, second)
    assert respond(roster, members[0], first_offer).status == "expired"


def test_opted_out_entries_keep_position_but_do_not_block_eligible_recipients(roster):
    _, factory, _ = roster
    members, slot, original = setup_queue(roster)
    first, second = [join(roster, m, slot) for m in members[:2]]
    with factory.begin() as db:
        db.get(Volunteer, members[0]).invite_opt_in = False
    free_slot(roster, original)
    assert dispatch(roster) == 1
    assert offer_for(roster, first) is None and offer_for(roster, second)


def test_opt_out_after_offer_releases_it_for_next_eligible_entry(roster):
    _, factory, _ = roster
    members, slot, original = setup_queue(roster)
    first, second = [join(roster, m, slot) for m in members[:2]]
    free_slot(roster, original)
    dispatch(roster)
    with factory.begin() as db:
        db.get(Volunteer, members[0]).invite_opt_in = False
    assert dispatch(roster) == 1
    with factory() as db:
        assert db.get(WaitlistEntry, first).status == "cancelled"
        assert db.get(WaitlistEntry, second).status == "offered"


def test_eligibility_is_rechecked_before_offer_and_after_capacity_is_claimed(roster):
    engine, factory, _ = roster
    members, slot, original = setup_queue(roster)
    first, second = [join(roster, m, slot) for m in members[:2]]
    with factory() as db:
        day = db.get(WorshipService, db.get(Slot, slot).service_id).service_date
    engine.set_unavailability(members[0], day, day)
    free_slot(roster, original)
    assert dispatch(roster) == 1
    with factory() as db:
        assert db.get(WaitlistEntry, first).status == "ineligible"
    engine.assign(members[2], slot)
    result = respond(roster, members[1], offer_for(roster, second))
    assert result.status == "cancelled"


def test_cannot_cancel_someone_elses_entry_or_confirmed_participation(roster):
    engine, factory, _ = roster
    members, slot, _ = setup_queue(roster, full=False)
    entry = join(roster, members[0], slot)
    with factory.begin() as db, pytest.raises(NotAuthorized):
        cancel_waitlist(db, engine, members[1], entry, CONFIG)
    dispatch(roster)
    respond(roster, members[0], offer_for(roster, entry))
    with factory.begin() as db, pytest.raises(RosterError, match="already decided"):
        cancel_waitlist(db, engine, members[0], entry, CONFIG)


def test_cancelled_entry_is_retained_and_rejoining_goes_to_the_end(roster):
    engine, factory, _ = roster
    members, slot, original = setup_queue(roster)
    first = join(roster, members[0], slot)
    second = join(roster, members[1], slot)
    with factory.begin() as db:
        cancel_waitlist(db, engine, members[0], first, CONFIG)
    third = join(roster, members[0], slot)
    assert first < second < third
    free_slot(roster, original)
    assert dispatch(roster) == 1
    assert offer_for(roster, second) and offer_for(roster, third) is None


def test_monthly_limits_and_unavailability_apply_even_to_a_full_slot(roster):
    engine, factory, ids = roster
    members, slot, _ = setup_queue(roster)
    with factory() as db:
        day = db.get(WorshipService, db.get(Slot, slot).service_id).service_date
    for offset in (3, 6):
        _, other = make_service(
            engine, factory, ids["ministry"], day + timedelta(days=offset), {ids["offer"]: 1}
        )
        engine.assign(members[0], other[ids["offer"]])
    with pytest.raises(MonthlyLimit):
        join(roster, members[0], slot)
    engine.set_unavailability(members[1], day, day)
    with pytest.raises(Unavailable):
        join(roster, members[1], slot)


def test_moving_the_service_invalidates_waiting_entries(roster):
    _, factory, _ = roster
    members, slot, original = setup_queue(roster)
    entry = join(roster, members[0], slot)
    free_slot(roster, original)
    with factory.begin() as db:
        db.get(WorshipService, db.get(Slot, slot).service_id).start_time = time(20)
    assert dispatch(roster) == 0
    with factory() as db:
        assert db.get(WaitlistEntry, entry).status == "ineligible"


def test_capacity_greater_than_one_offers_distinct_positions(roster):
    members, slot, _ = setup_queue(roster, full=False, capacity=2)
    entries = [join(roster, member, slot) for member in members]
    assert dispatch(roster) == 2
    assert all(offer_for(roster, entry) for entry in entries[:2])
    assert offer_for(roster, entries[2]) is None


def test_chat_shows_only_own_entries_and_can_withdraw(roster):
    _, factory, _ = roster
    _, slot, _ = setup_queue(roster)
    simulate(FIRST, "Oi", factory=factory, config=CONFIG)
    assert "Lista de espera #1" in bodies(
        simulate(FIRST, f"ESPERA {slot}", factory=factory, config=CONFIG)
    )
    simulate(SECOND, "Oi", factory=factory, config=CONFIG)
    assert "Nenhuma posição" in bodies(
        simulate(SECOND, "MINHAESPERA", factory=factory, config=CONFIG)
    )
    assert "waiting" in bodies(simulate(FIRST, "MINHAESPERA", factory=factory, config=CONFIG))
    assert "Saída" in bodies(simulate(FIRST, "SAIR_ESPERA 1", factory=factory, config=CONFIG))


def test_withdrawal_refreshes_an_offer_created_after_the_entry_was_loaded(roster):
    engine, factory, _ = roster
    members, slot, _ = setup_queue(roster, full=False)
    entry_id = join(roster, members[0], slot)
    with factory() as stale:
        loaded = stale.get(WaitlistEntry, entry_id)
        assert loaded.offer_id is None
        assert dispatch(roster) == 1
        offer_id = offer_for(roster, entry_id)
        cancel_waitlist(stale, engine, members[0], entry_id, CONFIG)
        stale.commit()
    with factory() as db:
        assert db.get(AutomationInvitation, offer_id).status == "cancelled"
    assert respond(roster, members[0], offer_id).status == "cancelled"


def test_unopened_schedules_cannot_be_used_to_join_a_waitlist(roster):
    _, factory, ids = roster
    members, slot, _ = setup_queue(roster, full=False)
    with factory.begin() as db:
        service = db.get(WorshipService, db.get(Slot, slot).service_id)
        db.delete(schedule_for(db, ids["ministry"], service.service_date))
    with pytest.raises(RosterError, match="open or published"):
        join(roster, members[0], slot)


def test_stateless_waitlist_command_works_as_first_message(roster):
    _, factory, _ = roster
    _, slot, _ = setup_queue(roster)
    assert "Lista de espera #1" in bodies(
        simulate(FIRST, f"ESPERA {slot}", factory=factory, config=CONFIG)
    )
