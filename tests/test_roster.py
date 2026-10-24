from datetime import date

import pytest
from pydantic import ValidationError

from church_roster_bot.members.models import Volunteer
from church_roster_bot.members.schemas import VolunteerCreate
from church_roster_bot.scheduling.errors import (
    MonthlyLimit,
    NoCapacity,
    NotAuthorized,
    RoleConflictError,
    RoleMismatch,
    RosterError,
    Unavailable,
)
from church_roster_bot.scheduling.models import Assignment, ThirdDayRequest
from tests.conftest import make_service


def add_member(engine, church, phone, roles, name="Pessoa Fictícia"):
    return engine.register_volunteer(
        church,
        VolunteerCreate(name=name, phone_e164=phone, role_ids=set(roles), invite_opt_in=True),
    )


def test_registration_and_role_permissions(roster):
    engine, factory, ids = roster
    member = add_member(engine, ids["church"], "+15550100001", [ids["open"]])
    with factory() as db:
        assert db.get(Volunteer, member).phone_e164 == "+15550100001"
    _, slots = make_service(
        engine, factory, ids["ministry"], date(2026, 11, 1), {ids["open"]: 1, ids["offer"]: 1}
    )
    engine.assign(member, slots[ids["open"]])
    with pytest.raises(RoleMismatch):
        engine.assign(member, slots[ids["offer"]])
    with pytest.raises(ValidationError):
        VolunteerCreate(name="Bad", phone_e164="11988880001", role_ids={ids["open"]})


def test_distinct_days_monthly_limit_and_reset(roster):
    engine, factory, ids = roster
    member = add_member(engine, ids["church"], "+15550100002", [ids["open"], ids["close"]])
    slots = []
    for day in (1, 4, 7):
        _, s = make_service(
            engine, factory, ids["ministry"], date(2026, 11, day), {ids["open"]: 1, ids["close"]: 1}
        )
        slots.append(s)
    engine.assign(member, slots[0][ids["open"]])
    engine.assign(member, slots[0][ids["close"]])
    assert engine.monthly_days(member, date(2026, 11, 1)) == 1
    engine.assign(member, slots[1][ids["open"]])
    with pytest.raises(MonthlyLimit):
        engine.assign(member, slots[2][ids["open"]])
    _, december = make_service(
        engine, factory, ids["ministry"], date(2026, 12, 2), {ids["open"]: 1}
    )
    engine.assign(member, december[ids["open"]])
    assert engine.monthly_days(member, date(2026, 12, 2)) == 1


def test_third_day_requires_coordinator_reason_and_volunteer_acceptance(roster):
    engine, factory, ids = roster
    member = add_member(engine, ids["church"], "+15550100003", [ids["open"]])
    slots = []
    for day in (1, 4, 7):
        _, s = make_service(engine, factory, ids["ministry"], date(2026, 11, day), {ids["open"]: 1})
        slots.append(s[ids["open"]])
    engine.assign(member, slots[0])
    engine.assign(member, slots[1])
    with pytest.raises(RosterError):
        engine.request_third_day(ids["coordinator"], member, slots[2], " ")
    with pytest.raises(NotAuthorized):
        engine.request_third_day(9999, member, slots[2], "Necessidade pontual")
    request_id = engine.request_third_day(
        ids["coordinator"], member, slots[2], "Necessidade pontual"
    )
    with factory() as db:
        assert db.get(ThirdDayRequest, request_id).status == "pending"
        assert engine.monthly_days(member, date(2026, 11, 7)) == 2
    assignment_id = engine.respond_third_day(member, request_id, True)
    with factory() as db:
        assignment = db.get(Assignment, assignment_id)
        assert assignment.exception_id == request_id
        assert db.get(ThirdDayRequest, request_id).justification == "Necessidade pontual"
    assert engine.monthly_days(member, date(2026, 11, 7)) == 3


def test_declined_third_day_does_not_assign(roster):
    engine, factory, ids = roster
    member = add_member(engine, ids["church"], "+15550100004", [ids["open"]])
    slots = []
    for day in (1, 4, 7):
        _, s = make_service(engine, factory, ids["ministry"], date(2026, 11, day), {ids["open"]: 1})
        slots.append(s[ids["open"]])
    engine.assign(member, slots[0])
    engine.assign(member, slots[1])
    request_id = engine.request_third_day(
        ids["coordinator"], member, slots[2], "Cobertura excepcional"
    )
    assert engine.respond_third_day(member, request_id, False) is None
    assert engine.monthly_days(member, date(2026, 11, 7)) == 2
    with pytest.raises(RosterError):
        engine.respond_third_day(member, request_id, True)


def test_role_conflicts_and_joint_role(roster):
    engine, factory, ids = roster
    member = add_member(
        engine,
        ids["church"],
        "+15550100005",
        [ids["open"], ids["close"], ids["offer"], ids["joint"]],
    )
    _, slots = make_service(
        engine,
        factory,
        ids["ministry"],
        date(2026, 11, 1),
        {ids["open"]: 1, ids["close"]: 1, ids["offer"]: 1, ids["joint"]: 1},
    )
    engine.assign(member, slots[ids["open"]])
    engine.assign(member, slots[ids["close"]])
    with pytest.raises(RoleConflictError):
        engine.assign(member, slots[ids["offer"]])
    other = add_member(engine, ids["church"], "+15550100006", [ids["joint"], ids["offer"]])
    engine.assign(other, slots[ids["joint"]])
    with pytest.raises(RoleConflictError):
        engine.assign(other, slots[ids["offer"]])


def test_full_slot_cannot_be_overbooked(roster):
    engine, factory, ids = roster
    a = add_member(engine, ids["church"], "+15550100007", [ids["offer"]])
    b = add_member(engine, ids["church"], "+15550100008", [ids["offer"]])
    _, slots = make_service(engine, factory, ids["ministry"], date(2026, 11, 1), {ids["offer"]: 1})
    engine.assign(a, slots[ids["offer"]])
    with pytest.raises(NoCapacity):
        engine.assign(b, slots[ids["offer"]])


def test_unavailability_blocks_new_assignment_and_reports_existing_conflict(roster):
    engine, factory, ids = roster
    member = add_member(engine, ids["church"], "+15550100009", [ids["offer"]])
    _, first = make_service(engine, factory, ids["ministry"], date(2026, 11, 1), {ids["offer"]: 1})
    assigned = engine.assign(member, first[ids["offer"]])
    assert engine.set_unavailability(member, date(2026, 11, 1), date(2026, 11, 8)) == [assigned]
    _, second = make_service(engine, factory, ids["ministry"], date(2026, 11, 4), {ids["offer"]: 1})
    with pytest.raises(Unavailable):
        engine.assign(member, second[ids["offer"]])


def test_capacity_warning_for_eight_people_and_seventeen_positions(roster):
    engine, factory, ids = roster
    for index in range(8):
        add_member(engine, ids["church"], f"+1555010{index:04d}", [ids["offer"]])
    _, _ = make_service(engine, factory, ids["ministry"], date(2026, 11, 1), {ids["offer"]: 8})
    _, _ = make_service(engine, factory, ids["ministry"], date(2026, 11, 4), {ids["offer"]: 8})
    _, _ = make_service(engine, factory, ids["ministry"], date(2026, 11, 7), {ids["offer"]: 1})
    report = engine.capacity_report(ids["ministry"], date(2026, 11, 1))
    assert report.needed_positions == 17
    assert report.normal_day_capacity == 16
    assert report.shortage_at_least == 1
    assert report.warnings


def test_capacity_counts_compatible_roles_as_one_day(roster):
    engine, factory, ids = roster
    add_member(engine, ids["church"], "+15550100099", [ids["open"], ids["close"]])
    for day in (1, 4):
        make_service(
            engine,
            factory,
            ids["ministry"],
            date(2026, 11, day),
            {ids["open"]: 1, ids["close"]: 1},
        )
    report = engine.capacity_report(ids["ministry"], date(2026, 11, 1))
    assert report.needed_positions == 4
    assert report.normal_day_capacity == 2
    assert report.shortage_at_least == 0
    assert not report.warnings
