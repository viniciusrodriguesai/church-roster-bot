"""Competing workers keep cancellation and its audit receipt idempotent."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from sqlalchemy import func, select

from church_roster_bot.audit.models import AuditEvent
from church_roster_bot.notifications.worker import lock_simulation_transaction
from church_roster_bot.scheduling.models import RosterProposal
from church_roster_bot.scheduling.proposals import cancel_proposal
from tests.test_distribution import prepare
from tests.test_proposals import create

pytestmark = pytest.mark.parametrize(
    "roster", ["sqlite_file", pytest.param("postgres", marks=pytest.mark.postgres)], indirect=True
)


def test_competing_cancellations_preserve_one_decision(roster):
    _, factory, ids = roster
    prepare(roster)
    proposal_id = create(roster)
    barrier = Barrier(2)

    def cancel(_):
        barrier.wait(timeout=10)
        with factory.begin() as db:
            lock_simulation_transaction(db)
            cancel_proposal(db, proposal_id, ids["ministry"], "coordinator", ids["coordinator"])

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(cancel, range(2)))
    with factory() as db:
        assert db.get(RosterProposal, proposal_id).status == "cancelled"
        assert (
            db.scalar(
                select(func.count(AuditEvent.id)).where(AuditEvent.action == "proposal_cancelled")
            )
            == 1
        )
