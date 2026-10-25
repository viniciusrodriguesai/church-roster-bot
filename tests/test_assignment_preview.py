"""Advisory validation shares the write rules without creating reservations."""

from datetime import date

import pytest
from sqlalchemy import event, func, select

from church_roster_bot.members.models import Volunteer
from church_roster_bot.scheduling.engine import AssignmentIntent
from church_roster_bot.scheduling.errors import (
    MonthlyLimit,
    NoCapacity,
    NotFound,
    RoleConflictError,
    RoleMismatch,
    RosterError,
    Unavailable,
)
from church_roster_bot.scheduling.lifecycle import schedule_for
from church_roster_bot.scheduling.models import Assignment
from tests.conftest import make_service
from tests.test_roster import add_member

pytestmark = pytest.mark.parametrize(
    "roster", ["sqlite", pytest.param("postgres", marks=pytest.mark.postgres)], indirect=True
)
DAY = date(2027, 1, 1)


def setup_slots(roster):
    engine, factory, ids = roster
    member = add_member(engine, ids["church"], "+15550100001", [ids["open"], ids["close"]])
    slots = []
    for day in (1, 4, 7):
        _, available = make_service(
            engine,
            factory,
            ids["ministry"],
            DAY.replace(day=day),
            {ids["open"]: 1, ids["close"]: 1, ids["offer"]: 1},
        )
        slots.append(available)
    return member, slots


def test_preview_is_read_only_even_with_projected_choices(roster):
    engine, factory, ids = roster
    member, slots = setup_slots(roster)
    statements = []

    def record(_conn, _cursor, statement, _parameters, _context, _many):
        statements.append(statement.strip().split()[0].upper())

    bind = factory.kw["bind"]
    event.listen(bind, "before_cursor_execute", record)
    try:
        with factory.begin() as db:
            engine.check_assignment(
                db,
                member,
                slots[1][ids["open"]],
                projected=[AssignmentIntent(member, slots[0][ids["open"]])],
            )
            assert not db.new and not db.dirty and not db.deleted
    finally:
        event.remove(bind, "before_cursor_execute", record)
    assert statements and set(statements) == {"SELECT"}
    with factory() as db:
        assert db.scalar(select(func.count(Assignment.id))) == 0


@pytest.mark.parametrize("rule", ["permission", "unavailable", "inactive", "closed"])
def test_preview_and_assignment_reject_the_same_ineligible_candidate(roster, rule):
    engine, factory, ids = roster
    member, slots = setup_slots(roster)
    target = slots[0][ids["open"]]
    error = RosterError
    if rule == "permission":
        target, error = slots[0][ids["offer"]], RoleMismatch
    elif rule == "unavailable":
        engine.set_unavailability(member, DAY, DAY)
        error = Unavailable
    elif rule == "inactive":
        with factory.begin() as db:
            db.get(Volunteer, member).active = False
        error = NotFound
    else:
        with factory.begin() as db:
            schedule_for(db, ids["ministry"], DAY, create=True).state = "closed"
    with factory() as db, pytest.raises(error):
        engine.check_assignment(db, member, target)
    with pytest.raises(error):
        engine.assign(member, target)


def test_projected_choices_consume_capacity_without_blocking_real_signup(roster):
    engine, factory, ids = roster
    member, slots = setup_slots(roster)
    other = add_member(engine, ids["church"], "+15550100002", [ids["open"]])
    target = slots[0][ids["open"]]
    with factory() as db, pytest.raises(NoCapacity):
        engine.check_assignment(db, other, target, projected=[AssignmentIntent(member, target)])
    assert engine.assign(other, target)


def test_persisted_and_projected_days_share_the_two_day_limit(roster):
    engine, factory, ids = roster
    member, slots = setup_slots(roster)
    engine.assign(member, slots[0][ids["open"]])
    projected = [AssignmentIntent(member, slots[1][ids["open"]])]
    with factory() as db:
        with pytest.raises(MonthlyLimit):
            engine.check_assignment(db, member, slots[2][ids["open"]], projected=projected)
        # Another compatible task on a projected day is still only one distinct day.
        engine.check_assignment(db, member, slots[1][ids["close"]], projected=projected)
    assert engine.monthly_days(member, DAY) == 1


def test_projected_incompatible_roles_and_duplicates_are_rejected(roster):
    engine, factory, ids = roster
    member = add_member(engine, ids["church"], "+15550100001", [ids["open"], ids["offer"]])
    _, slots = make_service(
        engine, factory, ids["ministry"], DAY, {ids["open"]: 2, ids["offer"]: 1}
    )
    projected = [AssignmentIntent(member, slots[ids["open"]])]
    with factory() as db:
        with pytest.raises(RoleConflictError):
            engine.check_assignment(db, member, slots[ids["offer"]], projected=projected)
        with pytest.raises(RosterError, match="already assigned"):
            engine.check_assignment(db, member, slots[ids["open"]], projected=projected)


def test_projection_in_another_month_does_not_consume_target_month_days(roster):
    engine, factory, ids = roster
    member, slots = setup_slots(roster)
    other_month = []
    for day in (1, 4):
        _, available = make_service(
            engine, factory, ids["ministry"], date(2026, 12, day), {ids["open"]: 1}
        )
        other_month.append(AssignmentIntent(member, available[ids["open"]]))
    with factory() as db:
        engine.check_assignment(db, member, slots[0][ids["open"]], projected=other_month)


def test_advisory_check_never_authorizes_a_third_day(roster):
    engine, factory, ids = roster
    member, slots = setup_slots(roster)
    for slot in slots[:2]:
        engine.assign(member, slot[ids["open"]])
    invitation = engine.request_third_day(
        ids["coordinator"], member, slots[2][ids["open"]], "Necessidade fictícia"
    )
    with factory() as db, pytest.raises(MonthlyLimit):
        engine.check_assignment(db, member, slots[2][ids["open"]])
    assert engine.respond_third_day(member, invitation, True)
