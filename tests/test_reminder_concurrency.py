"""Competing reminder schedulers and senders preserve one delivery per participation."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
from sqlalchemy import func, select

from church_roster_bot.notifications.worker import Worker
from church_roster_bot.scheduling.models import Assignment
from church_roster_bot.scheduling.reminders import enqueue_reminders
from church_roster_bot.whatsapp.models import OutboundMessage
from tests.test_automation_concurrency import run_race
from tests.test_invitation_delivery import RecordingClient, drain
from tests.test_phase2 import FIRST
from tests.test_reminders import CONFIG, NOW, notice, setup_reminder, tick
from tests.test_reminders import reminder_clock as reminder_clock

pytestmark = pytest.mark.parametrize(
    "roster", ["sqlite_file", pytest.param("postgres", marks=pytest.mark.postgres)], indirect=True
)


def test_two_schedulers_queue_exactly_one_reminder(roster):
    _, factory, _ = roster
    setup_reminder(roster)
    assert sorted(run_race(factory, lambda db, _: enqueue_reminders(db, CONFIG, now=NOW))) == [0, 1]
    with factory() as db:
        assert db.scalar(select(func.count(OutboundMessage.id))) == 1


def test_two_senders_do_not_repeat_one_reminder(roster):
    _, factory, _ = roster
    setup_reminder(roster)
    tick(roster)
    barrier = Barrier(2)
    client = RecordingClient()

    def send(_):
        barrier.wait(timeout=10)
        return Worker(factory, CONFIG, client).send_one()

    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sum(pool.map(send, range(2))) == 1
    assert sum(phone == FIRST for phone, _ in client.calls) == 1


def test_cancellation_racing_enqueue_cannot_deliver_a_stale_reminder(roster):
    _, factory, _ = roster
    _, _, assignment = setup_reminder(roster)

    def operation(db, index):
        if index:
            return enqueue_reminders(db, CONFIG, now=NOW)
        db.get(Assignment, assignment, with_for_update=True).status = "cancelled"

    run_race(factory, operation)
    client = drain(factory, CONFIG)
    assert not any(phone == FIRST for phone, _ in client.calls)
    assert notice(roster) is None or notice(roster).status == "cancelled"
