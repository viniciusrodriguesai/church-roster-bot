"""Claims must re-read protected rows, and administrative changes must serialize."""

import time as wall_time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, time, timedelta
from queue import Queue

import pytest
from sqlalchemy import event, select, text

from church_roster_bot.administration.service import edit_service, edit_volunteer
from church_roster_bot.members.models import Coordinator, Volunteer
from church_roster_bot.scheduling.errors import RosterError
from church_roster_bot.scheduling.lifecycle import publish_schedule, schedule_for, set_slot_capacity
from church_roster_bot.scheduling.models import MonthlySchedule
from church_roster_bot.services.models import Slot, WorshipService
from tests.test_phase2 import FIRST, SECOND, add

MONTH = (date.today().replace(day=1) + timedelta(days=65)).replace(day=1)


def service(roster, day=1, hour=19, capacity=2):
    engine, factory, ids = roster
    sid = engine.create_service(
        ids["ministry"], MONTH.replace(day=day), time(hour), {ids["offer"]: capacity}
    )
    with factory.begin() as db:
        schedule = schedule_for(db, ids["ministry"], MONTH, create=True)
        schedule.state = "open"
        slot = db.scalar(select(Slot.id).where(Slot.service_id == sid))
        return sid, slot, schedule.id


@pytest.mark.parametrize(
    "roster", ["sqlite_file", pytest.param("postgres", marks=pytest.mark.postgres)], indirect=True
)
@pytest.mark.parametrize("changed", ["member", "capacity", "service", "schedule"])
def test_claim_revalidates_cached_rows_after_another_transaction(roster, changed):
    engine, factory, ids = roster
    first = add(engine, ids, FIRST)
    member = add(engine, ids, SECOND)
    sid, slot, schedule = service(roster, hour=20)
    if changed == "capacity":
        engine.assign(first, slot)
    if changed == "service":
        for day in (1, 3):
            _, other_slot, _ = service(roster, day)
            engine.assign(member, other_slot)
    with factory.begin() as cached:
        model, identity = {
            "member": (Volunteer, member),
            "capacity": (Slot, slot),
            "service": (WorshipService, sid),
            "schedule": (MonthlySchedule, schedule),
        }[changed]
        snapshot = cached.get(model, identity)
        with factory.begin() as writer:
            if changed == "member":
                writer.get(Volunteer, member).active = False
            elif changed == "capacity":
                set_slot_capacity(writer, slot, 1)
            elif changed == "service":
                edit_service(
                    writer,
                    ids["ministry"],
                    sid,
                    "date",
                    MONTH.replace(day=6).isoformat(),
                    "coordinator",
                    ids["coordinator"],
                )
            else:
                writer.get(MonthlySchedule, schedule).state = "closed"
        assert snapshot is not None  # retain the actual stale identity-map object
        with pytest.raises(RosterError):
            engine.assign(member, slot, db=cached)


@pytest.mark.parametrize(
    "roster", ["sqlite_file", pytest.param("postgres", marks=pytest.mark.postgres)], indirect=True
)
def test_third_day_authorization_does_not_reuse_cached_coordinator_activity(roster):
    engine, factory, ids = roster
    member = add(engine, ids)
    for day in (1, 3):
        _, slot, _ = service(roster, day)
        engine.assign(member, slot)
    _, target, _ = service(roster, 6)
    with factory.begin() as cached:
        coordinator = cached.get(Coordinator, ids["coordinator"])
        with factory.begin() as revoked:
            revoked.get(Coordinator, ids["coordinator"]).active = False
        assert coordinator.active
        with pytest.raises(RosterError):
            engine.request_third_day(
                ids["coordinator"], member, target, "Fictitious audit reason", db=cached
            )


@pytest.mark.parametrize(
    "roster", ["sqlite_file", pytest.param("postgres", marks=pytest.mark.postgres)], indirect=True
)
def test_republication_updates_changed_name_without_overwriting_history(roster):
    engine, factory, ids = roster
    member = add(engine, ids)
    _, slot, _ = service(roster)
    engine.assign(member, slot)
    with factory.begin() as db:
        first = publish_schedule(db, ids["ministry"], MONTH)
    with factory.begin() as db:
        edit_volunteer(
            db,
            member,
            "name",
            "Novo Nome Fictício",
            ids["ministry"],
            "coordinator",
            ids["coordinator"],
        )
    with factory.begin() as db:
        second = publish_schedule(db, ids["ministry"], MONTH)
    assert second.version == first.version + 1
    assert "Novo Nome Fictício" in second.content
    assert "Pessoa Exemplo" in first.content


def wait_for_database_wait_or_completion(factory, pid, future):
    deadline = wall_time.monotonic() + 10
    with factory() as observer:
        while not future.done():
            observer.execute(text("SELECT pg_stat_clear_snapshot()"))
            if (
                observer.scalar(
                    text("SELECT wait_event_type FROM pg_stat_activity WHERE pid=:pid"),
                    {"pid": pid},
                )
                == "Lock"
            ):
                return
            assert wall_time.monotonic() < deadline, (
                "operation did not finish or wait on its row lock"
            )
            wall_time.sleep(0.01)


@pytest.mark.postgres
@pytest.mark.parametrize("roster", ["postgres"], indirect=True)
def test_service_move_racing_validated_signup_cannot_create_third_day(roster):
    engine, factory, ids = roster
    member = add(engine, ids)
    for day in (1, 3):
        _, slot, _ = service(roster, day)
        engine.assign(member, slot)
    sid, target, _ = service(roster, hour=20)
    backend = Queue()
    future = None

    def move():
        try:
            with factory.begin() as db:
                backend.put(db.scalar(text("SELECT pg_backend_pid()")))
                edit_service(
                    db,
                    ids["ministry"],
                    sid,
                    "date",
                    MONTH.replace(day=6).isoformat(),
                    "coordinator",
                    ids["coordinator"],
                )
            return "moved"
        except RosterError:
            return "rejected"

    with ThreadPoolExecutor(max_workers=1) as pool:

        def pause_before_insert(conn, cursor, statement, parameters, context, executemany):
            nonlocal future
            if future is None and statement.startswith("INSERT INTO assignments"):
                future = pool.submit(move)
                wait_for_database_wait_or_completion(factory, backend.get(timeout=10), future)

        bind = factory.kw["bind"]
        event.listen(bind, "before_cursor_execute", pause_before_insert)
        try:
            engine.assign(member, target)
        finally:
            event.remove(bind, "before_cursor_execute", pause_before_insert)
        assert future is not None and future.result(timeout=15) == "rejected"
    assert engine.monthly_days(member, MONTH) == 2
    with factory() as db:
        assert db.get(WorshipService, sid).service_date == MONTH


@pytest.mark.postgres
@pytest.mark.parametrize("roster", ["postgres"], indirect=True)
def test_publication_waits_for_claim_before_rendering_its_version(roster):
    engine, factory, ids = roster
    member = add(engine, ids)
    _, slot, _ = service(roster)
    backend = Queue()

    def publish():
        with factory.begin() as db:
            backend.put(db.scalar(text("SELECT pg_backend_pid()")))
            return publish_schedule(db, ids["ministry"], MONTH).content

    with ThreadPoolExecutor(max_workers=1) as pool:
        with factory.begin() as claiming:
            engine.assign(member, slot, db=claiming)
            future = pool.submit(publish)
            wait_for_database_wait_or_completion(factory, backend.get(timeout=10), future)
        content = future.result(timeout=15)
    assert "Pessoa Exemplo" in content
