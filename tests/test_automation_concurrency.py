"""Real PostgreSQL races plus serialized SQLite simulator workers."""

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from threading import Barrier

import pytest
from sqlalchemy import func, select

from church_roster_bot.audit.models import AuditEvent
from church_roster_bot.notifications.worker import Worker, lock_simulation_transaction
from church_roster_bot.scheduling.models import Assignment, AutomationInvitation, RosterProposal
from church_roster_bot.scheduling.offers import approve_proposal, respond_offer
from church_roster_bot.scheduling.proposals import create_proposal
from church_roster_bot.whatsapp.models import OutboundMessage
from tests.conftest import make_service
from tests.test_automation_offers import approve, setup_offer
from tests.test_invitation_delivery import RecordingClient

pytestmark = pytest.mark.parametrize(
    "roster", ["sqlite_file", pytest.param("postgres", marks=pytest.mark.postgres)], indirect=True
)


def run_race(factory, operation):
    barrier = Barrier(2)

    def run(index):
        barrier.wait(timeout=10)
        with factory.begin() as db:
            lock_simulation_transaction(db)
            return operation(db, index)

    with ThreadPoolExecutor(max_workers=2) as pool:
        return list(pool.map(run, range(2)))


def test_concurrent_approvals_create_one_invitation_and_one_outbound(roster):
    engine, factory, ids = roster
    _, _, proposal_id, config = setup_offer(roster)
    results = run_race(
        factory,
        lambda db, _: approve_proposal(
            db, engine, proposal_id, ids["ministry"], "coordinator", ids["coordinator"], config
        ),
    )
    assert results[0] == results[1]
    with factory() as db:
        assert db.scalar(select(func.count(AutomationInvitation.id))) == 1
        assert db.scalar(select(func.count(OutboundMessage.id))) == 1
        assert (
            db.scalar(
                select(func.count(AuditEvent.id)).where(AuditEvent.action == "proposal_approved")
            )
            == 1
        )


def test_concurrent_repeated_acceptance_creates_one_assignment_and_notice(roster):
    engine, factory, _ = roster
    members, _, proposal_id, config = setup_offer(roster)
    offer_id = approve(roster, proposal_id, config)[0]
    results = run_race(
        factory, lambda db, _: respond_offer(db, engine, members[0], offer_id, True, config)
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


def test_two_different_offers_cannot_assign_the_same_last_position(roster):
    engine, factory, ids = roster
    members, slots, proposal_id, config = setup_offer(roster)
    first_offer = approve(roster, proposal_id, config)[0]
    with factory() as db:
        month = db.get(RosterProposal, proposal_id).month
    _, other = make_service(
        engine, factory, ids["ministry"], month + timedelta(days=3), {ids["offer"]: 1}
    )
    engine.assign(members[0], other[ids["offer"]])
    with factory.begin() as db:
        second_proposal = create_proposal(
            db,
            engine,
            ids["ministry"],
            month,
            actor_type="coordinator",
            actor_id=ids["coordinator"],
        )
    second_offer = approve(roster, second_proposal, config)[0]
    candidates = [(members[0], first_offer), (members[1], second_offer)]
    results = run_race(
        factory, lambda db, i: respond_offer(db, engine, *candidates[i], True, config)
    )
    assert sorted(result.status for result in results) == ["accepted", "cancelled"]
    with factory() as db:
        assert (
            db.scalar(select(func.count(Assignment.id)).where(Assignment.slot_id == slots[0])) == 1
        )


def test_two_senders_do_not_deliver_the_same_offer_twice(roster):
    _, factory, _ = roster
    _, _, proposal_id, config = setup_offer(roster)
    approve(roster, proposal_id, config)
    client = RecordingClient()
    barrier = Barrier(2)

    def send(_):
        barrier.wait(timeout=10)
        return Worker(factory, config, client).send_one()

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sum(pool.map(send, range(2))) == 1
    assert len(client.calls) == 1
