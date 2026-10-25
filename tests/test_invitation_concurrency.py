"""PostgreSQL serializes concurrent invitations and durable delivery claims."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from sqlalchemy import func, select

from church_roster_bot.notifications.invitations import enqueue_invitation
from church_roster_bot.notifications.worker import Worker, lock_simulation_transaction
from church_roster_bot.scheduling.models import ThirdDayRequest
from church_roster_bot.whatsapp.models import OutboundMessage
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
