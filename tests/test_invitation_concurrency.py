"""PostgreSQL serializes concurrent invitations and durable delivery claims."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from sqlalchemy import func, select

from church_roster_bot.notifications.invitations import enqueue_invitation
from church_roster_bot.notifications.worker import Worker, lock_simulation_transaction
from church_roster_bot.scheduling.models import Assignment, ThirdDayRequest
from church_roster_bot.scripts.simulate import receive
from church_roster_bot.whatsapp.models import InboundEvent, OutboundMessage
from tests.test_invitation_delivery import confirm, drain, prepare
from tests.test_phase2 import FIRST

pytestmark = pytest.mark.parametrize(
    "roster",
    ["sqlite_file", pytest.param("postgres", marks=pytest.mark.postgres)],
    indirect=True,
)


def test_simultaneous_confirmations_reuse_invitation_and_outbox(roster):
    engine, factory, ids = roster
    member, slot, config = prepare(roster)
    barrier = Barrier(2)

    def confirm_once(_):
        barrier.wait(timeout=10)
        with factory.begin() as db:
            # The production coordinator handler holds these locks through outbox creation.
            lock_simulation_transaction(db)
            request_id = engine.request_third_day(
                ids["coordinator"], member, slot, "Necessidade", db=db
            )
            enqueue_invitation(db, db.get(ThirdDayRequest, request_id), config)
            return request_id

    with ThreadPoolExecutor(max_workers=2) as pool:
        request_ids = list(pool.map(confirm_once, range(2)))
    assert request_ids[0] == request_ids[1]
    with factory() as db:
        assert db.scalar(select(func.count(ThirdDayRequest.id))) == 1
        assert (
            db.scalar(
                select(func.count(OutboundMessage.id)).where(
                    OutboundMessage.invitation_id == request_ids[0]
                )
            )
            == 1
        )


def test_simultaneous_workers_cannot_send_the_invitation_twice(roster):
    _, factory, _ = roster
    _, _, config = prepare(roster)
    fake = drain(factory, config)
    invitation_id, _ = confirm(factory, config)
    fake.calls.clear()
    barrier = Barrier(2)

    def send_once(_):
        barrier.wait(timeout=10)
        return Worker(factory, config, fake).send_one()

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert all(pool.map(send_once, range(2)))
    drain(factory, config, fake)
    invitations = [payload for phone, payload in fake.calls if phone == FIRST]
    assert len(invitations) == 1
    with factory() as db:
        assert (
            db.scalar(
                select(OutboundMessage.attempts).where(
                    OutboundMessage.invitation_id == invitation_id
                )
            )
            == 1
        )


@pytest.mark.parametrize("responses", [1, 2])
def test_concurrent_acceptance_claims_are_idempotent(roster, responses):
    _, factory, _ = roster
    member, slot, config = prepare(roster)
    invitation_id, _ = confirm(factory, config)
    client = drain(factory, config)
    event_ids = [
        receive(FIRST, f"ACEITAR {invitation_id}", factory=factory) for _ in range(responses)
    ]
    barrier = Barrier(2)

    def process_once(_):
        barrier.wait(timeout=10)
        return Worker(factory, config, client).process_one()

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sum(pool.map(process_once, range(2))) == responses
    drain(factory, config, client)
    with factory() as db:
        for event_id in event_ids:
            event = db.get(InboundEvent, event_id)
            assert (event.status, event.attempts) == ("processed", 1)
        assert db.get(ThirdDayRequest, invitation_id).status == "accepted"
        assignment = db.scalars(
            select(Assignment).where(Assignment.exception_id == invitation_id)
        ).one()
        assert (assignment.volunteer_id, assignment.slot_id, assignment.status) == (
            member,
            slot,
            "confirmed",
        )
        notice = db.scalars(
            select(OutboundMessage).where(
                OutboundMessage.dedupe_key == f"invitation:{invitation_id}:aceito"
            )
        ).one()
        assert (notice.status, notice.attempts) == ("sent", 1)
        receipt = (notice.recipient_e164, notice.payload)
    assert client.calls.count(receipt) == 1
    calls = list(client.calls)
    drain(factory, config, client)
    assert client.calls == calls
