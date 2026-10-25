"""Queue admission, withdrawal and offers use the same total lock order."""

import pytest
from sqlalchemy import func, select

from church_roster_bot.scheduling.models import Assignment, AutomationInvitation, WaitlistEntry
from church_roster_bot.scheduling.offers import respond_offer
from church_roster_bot.scheduling.waitlist import cancel_waitlist, dispatch_waitlist, join_waitlist
from church_roster_bot.whatsapp.models import OutboundMessage
from tests.test_automation_concurrency import run_race
from tests.test_waitlist import CONFIG, dispatch, join, offer_for, setup_queue

pytestmark = pytest.mark.parametrize(
    "roster", ["sqlite_file", pytest.param("postgres", marks=pytest.mark.postgres)], indirect=True
)


def test_concurrent_joins_reuse_one_active_entry(roster):
    engine, factory, _ = roster
    members, slot, _ = setup_queue(roster)
    results = run_race(factory, lambda db, _: join_waitlist(db, engine, members[0], slot))
    assert results[0] == results[1]
    with factory() as db:
        assert db.scalar(select(func.count(WaitlistEntry.id))) == 1


def test_two_dispatchers_offer_one_position_once(roster):
    engine, factory, _ = roster
    members, slot, _ = setup_queue(roster, full=False)
    for member in members[:2]:
        join(roster, member, slot)
    results = run_race(factory, lambda db, _: dispatch_waitlist(db, engine, CONFIG))
    assert sorted(results) == [0, 1]
    with factory() as db:
        assert db.scalar(select(func.count(AutomationInvitation.id))) == 1
        assert db.scalar(select(func.count(OutboundMessage.id))) == 1


def test_withdrawal_racing_dispatch_never_leaves_an_acceptable_offer(roster):
    engine, factory, _ = roster
    members, slot, _ = setup_queue(roster, full=False)
    entry = join(roster, members[0], slot)

    def operation(db, index):
        if index:
            return dispatch_waitlist(db, engine, CONFIG)
        return cancel_waitlist(db, engine, members[0], entry, CONFIG)

    run_race(factory, operation)
    with factory() as db:
        assert db.get(WaitlistEntry, entry).status == "cancelled"
        assert (
            db.scalar(
                select(func.count(AutomationInvitation.id)).where(
                    AutomationInvitation.status == "pending"
                )
            )
            == 0
        )
        assert db.scalar(select(func.count(Assignment.id))) == 0


def test_decline_racing_dispatch_eventually_offers_the_next_entry_once(roster):
    engine, factory, _ = roster
    members, slot, _ = setup_queue(roster, full=False)
    first, second = [join(roster, member, slot) for member in members[:2]]
    dispatch(roster)
    offer = offer_for(roster, first)

    def operation(db, index):
        if index:
            return dispatch_waitlist(db, engine, CONFIG)
        return respond_offer(db, engine, members[0], offer, False, CONFIG)

    run_race(factory, operation)
    dispatch(roster)
    with factory() as db:
        assert db.get(WaitlistEntry, second).status == "offered"
        assert db.scalar(select(func.count(AutomationInvitation.id))) == 2
