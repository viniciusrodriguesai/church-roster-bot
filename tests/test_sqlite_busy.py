"""A busy simulator database must not stop input or resend an external message."""

import threading
from queue import Queue

import pytest
from sqlalchemy import event, select
from sqlalchemy.exc import OperationalError

from church_roster_bot.core.database import is_sqlite_busy, retry_sqlite_transaction
from church_roster_bot.notifications.worker import Worker, lock_simulation_transaction
from church_roster_bot.scripts.simulate import receive, run_terminal
from church_roster_bot.whatsapp.models import InboundEvent, OutboundMessage
from church_roster_bot.whatsapp.payloads import text_payload
from tests.test_distribution import prepare
from tests.test_invitation_delivery import RecordingClient
from tests.test_phase2 import FIRST

pytestmark = pytest.mark.parametrize("roster", ["sqlite_file"], indirect=True)


@pytest.fixture
def fast_busy(roster):
    _, factory, _ = roster
    bind = factory.kw["bind"]

    def checkout(dbapi, _record, _proxy):
        dbapi.execute("PRAGMA busy_timeout=1")

    event.listen(bind, "checkout", checkout)
    try:
        yield factory
    finally:
        event.remove(bind, "checkout", checkout)


def test_busy_claims_leave_the_event_and_attempts_unchanged_then_resume(roster, fast_busy):
    prepare(roster)
    event_id = receive(FIRST, "Oi", factory=fast_busy)
    worker = Worker(fast_busy, client=RecordingClient())
    with fast_busy.begin() as owner:
        lock_simulation_transaction(owner)
        assert not worker.maintain()
        assert not worker.process_one()
        assert not worker.send_one()
    with fast_busy() as db:
        receipt = db.get(InboundEvent, event_id)
        assert (receipt.status, receipt.attempts, receipt.error_code) == ("pending", 0, None)
    assert worker.process_one()
    with fast_busy() as db:
        assert db.get(InboundEvent, event_id).attempts == 1


def test_terminal_stays_alive_and_preserves_input_while_another_writer_holds_the_db(
    roster, fast_busy
):
    prepare(roster)
    inputs = Queue()
    waiting, received, retried = threading.Event(), threading.Event(), threading.Event()
    output, errors = [], []

    def read(_prompt):
        waiting.set()
        value = inputs.get(timeout=15)
        if value is None:
            raise EOFError
        return value

    def write(value):
        output.append(value)
        if "ESCALAS DA IGREJA" in value:
            received.set()

    def terminal():
        try:
            run_terminal(FIRST, factory=fast_busy, read=read, write=write, interval=0.01)
        except Exception as exc:
            errors.append(exc)

    # The checkout hook detects a second connection actually trying the blocked DB.
    bind = fast_busy.kw["bind"]

    def attempt(_conn, _cursor, statement, _parameters, _context, _many):
        if statement.startswith("INSERT INTO inbound_events"):
            retried.set()

    event.listen(bind, "before_cursor_execute", attempt)
    thread = threading.Thread(target=terminal, daemon=True)
    try:
        with fast_busy.begin() as owner:
            lock_simulation_transaction(owner)
            thread.start()
            assert waiting.wait(5)
            inputs.put("Oi")
            assert retried.wait(5)
            assert thread.is_alive() and not errors
        assert received.wait(10), (output, errors)
        assert not errors
    finally:
        inputs.put(None)
        thread.join(10)
        event.remove(bind, "before_cursor_execute", attempt)
    assert not thread.is_alive() and not errors


def test_a_busy_receipt_after_transport_success_never_repeats_the_send(
    roster, fast_busy, monkeypatch
):
    _, _, _ = roster
    with fast_busy.begin() as db:
        db.add(OutboundMessage(recipient_e164=FIRST, payload=text_payload("Olá")))
    locked, release, retrying = threading.Event(), threading.Event(), threading.Event()
    calls, failures = [], []

    def writer():
        try:
            with fast_busy.begin() as db:
                lock_simulation_transaction(db)
                locked.set()
                assert release.wait(10)
        except Exception as exc:
            failures.append(exc)

    owner_thread = threading.Thread(target=writer, daemon=True)

    class Client:
        def send(self, phone, payload):
            calls.append((phone, payload))
            owner_thread.start()
            assert locked.wait(5)
            return "wamid.synthetic-once"

    def pause(_duration):
        retrying.set()
        release.set()

    monkeypatch.setattr("church_roster_bot.core.database.time.sleep", pause)
    # A recent inbound opens the normal text window for this synthetic recipient.
    receive(FIRST, "Oi", factory=fast_busy)
    worker = Worker(fast_busy, client=Client())
    worker.process_one()
    try:
        assert worker.send_one()
    finally:
        release.set()
        owner_thread.join(10)
    assert retrying.is_set() and len(calls) == 1 and not failures
    with fast_busy() as db:
        outgoing = db.scalar(
            select(OutboundMessage).where(OutboundMessage.meta_message_id == "wamid.synthetic-once")
        )
        assert (outgoing.status, outgoing.attempts) == ("sent", 1)


def test_other_operational_errors_are_not_hidden_or_retried(roster):
    count = []

    def fail():
        count.append(True)
        raise OperationalError("SELECT", {}, RuntimeError("not SQLite contention"))

    with pytest.raises(OperationalError) as caught:
        retry_sqlite_transaction(fail)
    assert not is_sqlite_busy(caught.value) and len(count) == 1
