"""Emergency allocation interoperates with FIFO admission and competing assignment claims."""

import pytest
from sqlalchemy import func, select

from church_roster_bot.scheduling.emergencies import dispatch_emergencies
from church_roster_bot.scheduling.errors import RosterError
from church_roster_bot.scheduling.models import Assignment, AutomationInvitation
from church_roster_bot.scheduling.offers import respond_offer
from church_roster_bot.scheduling.waitlist import dispatch_waitlist, join_waitlist
from church_roster_bot.whatsapp.models import OutboundMessage
from tests.test_automation_concurrency import run_race
from tests.test_emergencies import CONFIG, NOW, offer, setup_emergency, tick

pytestmark = pytest.mark.parametrize(
    "roster", ["sqlite_file", pytest.param("postgres", marks=pytest.mark.postgres)], indirect=True
)


def test_two_emergency_dispatchers_create_one_offer(roster):
    engine, factory, _ = roster
    setup_emergency(roster)
    assert sorted(
        run_race(factory, lambda db, _: dispatch_emergencies(db, engine, CONFIG, now=NOW))
    ) == [0, 1]
    with factory() as db:
        assert db.scalar(select(func.count(AutomationInvitation.id))) == 1


def test_emergency_and_fifo_dispatchers_preserve_queue_precedence(roster):
    engine, factory, _ = roster
    members, slot = setup_emergency(roster)
    with factory.begin() as db:
        join_waitlist(db, engine, members[1], slot)

    def operation(db, index):
        if index:
            return dispatch_waitlist(db, engine, CONFIG, now=NOW)
        return dispatch_emergencies(db, engine, CONFIG, now=NOW)

    run_race(factory, operation)
    assert offer(roster).kind == "waitlist" and offer(roster).volunteer_id == members[1]
    with factory() as db:
        assert db.scalar(select(func.count(AutomationInvitation.id))) == 1


def test_repeated_concurrent_emergency_acceptance_confirms_once(roster):
    engine, factory, _ = roster
    members, _ = setup_emergency(roster)
    tick(roster)
    offer_id = offer(roster).id
    results = run_race(
        factory,
        lambda db, _: respond_offer(db, engine, members[0], offer_id, True, CONFIG, now=NOW),
    )
    assert results[0] == results[1] and results[0].status == "accepted"
    with factory() as db:
        assert db.scalar(select(func.count(Assignment.id))) == 1
        assert (
            db.scalar(
                select(func.count(OutboundMessage.id)).where(
                    OutboundMessage.dedupe_key.like(f"automation:{offer_id}:accepted:%")
                )
            )
            == 1
        )


def test_direct_signup_racing_urgent_acceptance_never_overbooks(roster):
    engine, factory, _ = roster
    members, slot = setup_emergency(roster)
    tick(roster)
    offer_id = offer(roster).id

    def operation(db, index):
        if index:
            try:
                return engine.assign(members[1], slot, db=db)
            except RosterError:
                return None
        return respond_offer(db, engine, members[0], offer_id, True, CONFIG, now=NOW)

    run_race(factory, operation)
    with factory() as db:
        assert db.scalar(select(func.count(Assignment.id)).where(Assignment.slot_id == slot)) == 1
