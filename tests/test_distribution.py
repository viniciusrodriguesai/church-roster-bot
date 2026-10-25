"""Balance proposals against current and projected work without persisting effects."""

from collections import Counter
from datetime import UTC, date, datetime, timedelta

import pytest
from sqlalchemy import event, func, select

from church_roster_bot.members.models import (
    Coordinator,
    CoordinatorMinistry,
    GeneralAdmin,
    Volunteer,
    VolunteerRole,
)
from church_roster_bot.ministries.models import Church, Ministry, Role
from church_roster_bot.scheduling.distribution import DistributionPlanner
from church_roster_bot.scheduling.errors import NotAuthorized, RosterError
from church_roster_bot.scheduling.lifecycle import schedule_for
from church_roster_bot.scheduling.models import Assignment, ThirdDayRequest
from church_roster_bot.scripts.simulate import simulate
from church_roster_bot.services.models import Slot
from church_roster_bot.whatsapp.models import OutboundMessage
from tests.conftest import make_service
from tests.test_phase2 import COORD, FIRST, bodies
from tests.test_roster import add_member

pytestmark = pytest.mark.parametrize(
    "roster", ["sqlite", pytest.param("postgres", marks=pytest.mark.postgres)], indirect=True
)
MONTH = date(2027, 1, 1)
NOW = datetime(2026, 12, 20, 12, tzinfo=UTC)


def prepare(roster, days=(1,), roles=None, members=2, month=MONTH):
    engine, factory, ids = roster
    roles = roles or [ids["offer"]]
    volunteers = [
        add_member(engine, ids["church"], f"+1555010{i:04d}", roles) for i in range(1, members + 1)
    ]
    slots = []
    for day in days:
        _, available = make_service(
            engine, factory, ids["ministry"], month.replace(day=day), {role: 1 for role in roles}
        )
        slots.extend(available.values())
    with factory.begin() as db:
        schedule_for(db, ids["ministry"], month, create=True).state = "open"
    return volunteers, slots


def propose(roster, **kwargs):
    engine, factory, ids = roster
    with factory() as db:
        return DistributionPlanner(engine).propose(
            db,
            ids["ministry"],
            MONTH,
            actor_type="coordinator",
            actor_id=ids["coordinator"],
            now=kwargs.pop("now", NOW),
            **kwargs,
        )


def test_balanced_explainable_deterministic_proposal_only_reads_database(roster):
    engine, factory, _ = roster
    members, _ = prepare(roster, days=(1, 4, 7, 10))
    statements = []

    def record(_conn, _cursor, statement, _parameters, _context, _many):
        statements.append(statement.strip().split()[0].upper())

    bind = factory.kw["bind"]
    event.listen(bind, "before_cursor_execute", record)
    try:
        first, second = propose(roster), propose(roster)
    finally:
        event.remove(bind, "before_cursor_execute", record)
    assert first == second
    assert set(statements) == {"SELECT"}
    assert Counter(s.intent.volunteer_id for s in first.suggestions) == dict.fromkeys(members, 2)
    assert not first.unfilled
    assert all("carga projetada" in s.score.explanation for s in first.suggestions)
    with factory() as db:
        for model in (Assignment, OutboundMessage, ThirdDayRequest):
            assert db.scalar(select(func.count(model.id))) == 0


def test_projected_third_day_is_left_unfilled_instead_of_authorized(roster):
    _, slots = prepare(roster, days=(1, 4, 7), members=1)
    proposal = propose(roster)
    assert len(proposal.suggestions) == 2
    assert len(proposal.unfilled) == 1
    assert proposal.unfilled[0].slot_id == slots[2]
    assert proposal.unfilled[0].positions == 1
    assert proposal.unfilled[0].rejections == (("MonthlyLimit", 1),)


def test_recent_history_prefers_less_used_volunteer(roster):
    engine, factory, ids = roster
    members, _ = prepare(roster)
    _, history = make_service(
        engine, factory, ids["ministry"], date(2026, 12, 1), {ids["offer"]: 1}
    )
    engine.assign(members[0], history[ids["offer"]])
    proposal = propose(roster)
    assert proposal.suggestions[0].intent.volunteer_id == members[1]
    assert proposal.suggestions[0].score.historical_days == 0


@pytest.mark.parametrize("status", ["cancelled", "replaced", "declined", "absent"])
def test_nonparticipation_is_excluded_from_history(roster, status):
    engine, factory, ids = roster
    members, _ = prepare(roster)
    _, history = make_service(
        engine, factory, ids["ministry"], date(2026, 12, 1), {ids["offer"]: 1}
    )
    assignment = engine.assign(members[0], history[ids["offer"]])
    with factory.begin() as db:
        db.get(Assignment, assignment).status = status
    choice = propose(roster).suggestions[0]
    assert choice.intent.volunteer_id == members[0]
    assert choice.score.historical_days == 0 and choice.score.last_served is None


def test_history_has_a_bounded_window_and_excludes_future_services(roster):
    engine, factory, ids = roster
    members, _ = prepare(roster)
    for day in (date(2026, 8, 1), date(2026, 12, 30)):
        _, history = make_service(engine, factory, ids["ministry"], day, {ids["offer"]: 1})
        engine.assign(members[0], history[ids["offer"]])
    assert propose(roster).suggestions[0].intent.volunteer_id == members[0]


def test_equally_used_volunteers_prefer_the_older_last_participation(roster):
    engine, factory, ids = roster
    members, _ = prepare(roster)
    for member, day in zip(members, (10, 1), strict=True):
        _, history = make_service(
            engine, factory, ids["ministry"], date(2026, 12, day), {ids["offer"]: 1}
        )
        engine.assign(member, history[ids["offer"]])
    assert propose(roster).suggestions[0].intent.volunteer_id == members[1]


def test_rare_role_is_planned_first_and_conflicts_are_respected(roster):
    engine, factory, ids = roster
    flexible = add_member(engine, ids["church"], "+15550100001", [ids["open"], ids["offer"]])
    specialist = add_member(engine, ids["church"], "+15550100002", [ids["offer"]])
    _, slots = make_service(
        engine, factory, ids["ministry"], MONTH, {ids["offer"]: 1, ids["open"]: 1}
    )
    with factory.begin() as db:
        schedule_for(db, ids["ministry"], MONTH, create=True).state = "open"
    proposal = propose(roster)
    assert [s.intent.volunteer_id for s in proposal.suggestions] == [flexible, specialist]
    assert [s.intent.slot_id for s in proposal.suggestions] == [
        slots[ids["open"]],
        slots[ids["offer"]],
    ]


def test_compatible_roles_on_same_date_count_only_one_day(roster):
    _, slots = prepare(
        roster, days=(1, 4), roles=[roster[2]["open"], roster[2]["close"]], members=1
    )
    proposal = propose(roster)
    assert len(proposal.suggestions) == len(slots) == 4
    assert not proposal.unfilled
    assert proposal.suggestions[-1].score.monthly_days == 2


def test_ineligible_candidates_do_not_appear_and_existing_assignments_are_preserved(roster):
    engine, factory, ids = roster
    members, slots = prepare(roster, days=(1, 4), members=3)
    engine.assign(members[0], slots[0])
    engine.set_unavailability(members[1], MONTH, MONTH.replace(day=31))
    with factory.begin() as db:
        db.get(Volunteer, members[2]).active = False
    proposal = propose(roster)
    assert len(proposal.suggestions) == 1
    assert proposal.suggestions[0].intent.volunteer_id == members[0]
    assert proposal.suggestions[0].intent.slot_id == slots[1]
    with factory() as db:
        assert db.scalars(select(Assignment.slot_id)).all() == [slots[0]]


def test_work_in_another_ministry_counts_towards_global_monthly_limit(roster):
    engine, factory, ids = roster
    members, _ = prepare(roster, members=1)
    with factory.begin() as db:
        ministry = Ministry(church_id=ids["church"], name="Outro ministério fictício")
        db.add(ministry)
        db.flush()
        role = Role(ministry_id=ministry.id, name="Função fictícia")
        db.add(role)
        db.flush()
        db.add(VolunteerRole(volunteer_id=members[0], role_id=role.id))
        ministry_id, role_id = ministry.id, role.id
    for day in (4, 7):
        _, available = make_service(
            engine, factory, ministry_id, MONTH.replace(day=day), {role_id: 1}
        )
        engine.assign(members[0], available[role_id])
    proposal = propose(roster)
    assert not proposal.suggestions
    assert proposal.unfilled[0].rejections == (("MonthlyLimit", 1),)


@pytest.mark.parametrize("state", ["draft", "under_review", "published", "closed", "cancelled"])
def test_only_open_schedules_can_be_proposed(roster, state):
    _, factory, ids = roster
    prepare(roster)
    with factory.begin() as db:
        schedule_for(db, ids["ministry"], MONTH).state = state
    with pytest.raises(RosterError, match="open monthly schedule"):
        propose(roster)


@pytest.mark.parametrize("change", ["inactive", "different_church", "no_ministry"])
def test_proposals_require_current_coordinator_authorization(roster, change):
    _, factory, ids = roster
    prepare(roster)
    with factory.begin() as db:
        coordinator = db.get(Coordinator, ids["coordinator"])
        if change == "inactive":
            coordinator.active = False
        elif change == "different_church":
            other = Church(name="Outra igreja fictícia")
            db.add(other)
            db.flush()
            coordinator.church_id = other.id
        else:
            db.delete(db.scalar(select(CoordinatorMinistry)))
    with pytest.raises(NotAuthorized):
        propose(roster)


def test_authorized_general_admin_can_propose(roster):
    engine, factory, ids = roster
    prepare(roster)
    with factory.begin() as db:
        admin = GeneralAdmin(church_id=ids["church"], phone_e164="+15550109999")
        db.add(admin)
        db.flush()
        admin_id = admin.id
    with factory() as db:
        assert (
            DistributionPlanner(engine)
            .propose(db, ids["ministry"], MONTH, actor_type="admin", actor_id=admin_id, now=NOW)
            .suggestions
        )


def test_past_services_use_the_church_timezone_and_naive_clocks_are_rejected(roster):
    _, factory, ids = roster
    prepare(roster)
    with factory.begin() as db:
        db.get(Church, ids["church"]).timezone = "Pacific/Auckland"
    # 06:00 UTC is 19:00 on Jan 1 in Auckland: the first service has already begun.
    proposal = propose(roster, now=datetime(2027, 1, 1, 6, tzinfo=UTC))
    assert not proposal.suggestions and not proposal.unfilled
    with pytest.raises(RosterError, match="timezone"):
        propose(roster, now=datetime(2027, 1, 1, 6))


def test_slot_capacity_greater_than_one_has_distinct_suggestions(roster):
    _, factory, _ = roster
    members, slots = prepare(roster, members=3)
    with factory.begin() as db:
        db.get(Slot, slots[0]).capacity = 3
    choices = propose(roster).suggestions
    assert {s.intent.volunteer_id for s in choices} == set(members)
    assert len(choices) == 3


def test_empty_month_is_a_valid_empty_proposal(roster):
    prepare(roster, days=())
    proposal = propose(roster)
    assert not proposal.suggestions and not proposal.unfilled


def test_other_church_volunteers_are_never_suggested(roster):
    _, factory, ids = roster
    prepare(roster, members=1)
    with factory.begin() as db:
        other = Church(name="Igreja fora do escopo")
        db.add(other)
        db.flush()
        db.scalar(select(Volunteer)).church_id = other.id
    assert not propose(roster).suggestions


def test_coordinator_can_request_a_preview_in_the_real_router_without_assigning(roster):
    _, factory, _ = roster
    month = (date.today().replace(day=1) + timedelta(days=32)).replace(day=1)
    prepare(roster, month=month)
    simulate(COORD, "Oi", factory=factory)
    command = f"PROPOR {month:%Y-%m}"
    first = simulate(COORD, command, factory=factory)
    assert "PRÉVIA DE DISTRIBUIÇÃO" in bodies(first)
    assert "Motivo: carga projetada" in bodies(first)
    assert "Nenhuma inscrição ou convite foi criado" in bodies(first)
    second = simulate(COORD, command, factory=factory)
    assert bodies(first) == bodies(second)
    with factory() as db:
        assert db.scalar(select(func.count(Assignment.id))) == 0
        assert db.scalar(select(func.count(ThirdDayRequest.id))) == 0


def test_volunteer_cannot_access_distribution_data_through_chat(roster):
    _, factory, _ = roster
    prepare(roster)
    simulate(FIRST, "Oi", factory=factory)
    replies = simulate(FIRST, "PROPOR 2027-01", factory=factory)
    assert "PRÉVIA DE DISTRIBUIÇÃO" not in bodies(replies)


def test_invalid_month_returns_a_domain_reply_without_changes(roster):
    _, factory, _ = roster
    prepare(roster)
    simulate(COORD, "Oi", factory=factory)
    replies = simulate(COORD, "PROPOR 2027-13", factory=factory)
    assert "Não foi possível concluir" in bodies(replies)
    with factory() as db:
        assert db.scalar(select(func.count(Assignment.id))) == 0


def test_query_cost_does_not_revalidate_the_entire_sql_matrix_after_each_choice(roster):
    _, factory, _ = roster
    prepare(roster, days=(1, 4, 7, 10, 13, 16), members=3)
    queries = []

    def record(_conn, _cursor, statement, _parameters, _context, _many):
        queries.append(statement)

    bind = factory.kw["bind"]
    event.listen(bind, "before_cursor_execute", record)
    try:
        proposal = propose(roster)
    finally:
        event.remove(bind, "before_cursor_execute", record)
    assert len(proposal.suggestions) == 6
    assert not proposal.unfilled
    # The original full matrix recomputation issued over 500 queries here.
    # Allow headroom for shared rule validation while rejecting that growth.
    assert len(queries) <= 300, len(queries)


def test_every_suggestion_can_pass_the_real_write_rules_when_state_is_unchanged(roster):
    engine, factory, _ = roster
    prepare(roster, days=(1, 4, 7, 10), members=2)
    proposal = propose(roster)
    with factory.begin() as db:
        for suggestion in proposal.suggestions:
            engine.assign(suggestion.intent.volunteer_id, suggestion.intent.slot_id, db=db)
    with factory() as db:
        assert db.scalar(select(func.count(Assignment.id))) == len(proposal.suggestions) == 4
