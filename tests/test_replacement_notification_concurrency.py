"""Original-recipient FK checks must respect replacement discovery lock order."""

import time
from concurrent.futures import ThreadPoolExecutor
from queue import Queue

import pytest
from sqlalchemy import event, func, select, text

from church_roster_bot.members.models import Volunteer
from church_roster_bot.scheduling.models import Assignment, AutomationInvitation, ReplacementRequest
from church_roster_bot.scheduling.offers import respond_offer
from church_roster_bot.scheduling.replacements import dispatch_replacements
from church_roster_bot.whatsapp.models import OutboundMessage
from tests.test_automatic_replacements import CONFIG, dispatch, pending_offer, setup_replacement


@pytest.mark.postgres
@pytest.mark.parametrize("roster", ["postgres"], indirect=True)
def test_replacement_notice_does_not_deadlock_candidate_discovery(roster):
    engine, factory, _ = roster
    members, _, original, request_id = setup_replacement(roster)
    dispatch(roster)
    offer = pending_offer(roster)
    backend = Queue()

    def accept():
        with factory.begin() as db:
            db.execute(text("SET LOCAL lock_timeout = '10s'"))
            backend.put(db.scalar(text("SELECT pg_backend_pid()")))
            return respond_offer(db, engine, members[1], offer.id, True, CONFIG)

    with ThreadPoolExecutor(max_workers=1) as pool:
        with factory.begin() as db:
            db.execute(text("SET LOCAL lock_timeout = '10s'"))
            # Pause discovery after its first member lock. Acceptance must wait
            # here before taking the substitute, slot or invitation locks.
            db.get(Volunteer, members[0], with_for_update=True)
            result = pool.submit(accept)
            pid = backend.get(timeout=10)
            deadline = time.monotonic() + 10
            with factory() as observer:
                while not observer.scalar(
                    text(
                        "SELECT EXISTS (SELECT 1 FROM pg_stat_activity "
                        "WHERE pid=:pid AND wait_event_type='Lock')"
                    ),
                    {"pid": pid},
                ):
                    assert time.monotonic() < deadline, "acceptance never reached its lock wait"
                    # Refresh PostgreSQL's per-transaction statistics snapshot.
                    observer.execute(text("SELECT pg_stat_clear_snapshot()"))
                    time.sleep(0.01)
            assert dispatch_replacements(db, engine, CONFIG) == 0
        decision = result.result(timeout=15)

    assert decision.status == "accepted"
    with factory() as db:
        assert db.get(Assignment, original).status == "replaced"
        assert db.get(ReplacementRequest, request_id).status == "fulfilled"
        assert (
            db.scalar(select(func.count(Assignment.id)).where(Assignment.status == "confirmed"))
            == 1
        )


@pytest.mark.postgres
@pytest.mark.parametrize("roster", ["postgres"], indirect=True)
def test_discovery_preserves_acceptance_committed_after_its_initial_snapshot(roster):
    engine, factory, _ = roster
    members, _, _, request_id = setup_replacement(roster)
    dispatch(roster)
    offer = pending_offer(roster)
    accepted = False

    def accept_before_discovery_locks(conn, cursor, statement, parameters, context, executemany):
        nonlocal accepted
        if (
            not accepted
            and "WHERE volunteers.church_id IN" in statement
            and "FOR UPDATE" in statement
        ):
            accepted = True
            with factory.begin() as other:
                assert (
                    respond_offer(other, engine, members[1], offer.id, True, CONFIG).status
                    == "accepted"
                )

    bind = factory.kw["bind"]
    event.listen(bind, "before_cursor_execute", accept_before_discovery_locks)
    try:
        with factory.begin() as db:
            assert dispatch_replacements(db, engine, CONFIG) == 0
    finally:
        event.remove(bind, "before_cursor_execute", accept_before_discovery_locks)
    assert accepted, "acceptance must commit between discovery's snapshot and member locks"
    with factory() as db:
        assert db.get(AutomationInvitation, offer.id).status == "accepted"
        assert db.get(ReplacementRequest, request_id).status == "fulfilled"
        assert not db.scalar(
            select(func.count(OutboundMessage.id)).where(
                OutboundMessage.dedupe_key.like(f"automation:{offer.id}:cancelled:%")
            )
        )
        assert (
            db.scalar(
                select(func.count(OutboundMessage.id)).where(
                    OutboundMessage.volunteer_id == members[0],
                    OutboundMessage.dedupe_key.like("member:%:fulfilled"),
                )
            )
            == 1
        )
