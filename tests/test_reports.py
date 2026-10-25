"""Read-only ministry reports distinguish commitments, recorded attendance and audit scope."""

from datetime import date, timedelta

import pytest
from sqlalchemy import event, select

from church_roster_bot.audit.models import AuditEvent
from church_roster_bot.core.config import Settings
from church_roster_bot.members.models import (
    CoordinatorMinistry,
    GeneralAdmin,
    Volunteer,
    VolunteerRole,
)
from church_roster_bot.ministries.models import Church, Ministry, Role
from church_roster_bot.scheduling.errors import NotAuthorized, RosterError
from church_roster_bot.scheduling.invitations import create_invitation
from church_roster_bot.scheduling.lifecycle import (
    audit,
    manual_replace,
    request_replacement,
    schedule_for,
)
from church_roster_bot.scheduling.models import Assignment
from church_roster_bot.scheduling.offers import respond_offer
from church_roster_bot.scheduling.reports import audit_history, monthly_report
from church_roster_bot.scripts.simulate import simulate
from tests.conftest import make_service
from tests.test_distribution import prepare
from tests.test_phase2 import COORD, FIRST, bodies

pytestmark = pytest.mark.parametrize(
    "roster", ["sqlite", pytest.param("postgres", marks=pytest.mark.postgres)], indirect=True
)
MONTH = date(2027, 1, 1)


def report(roster, month=MONTH):
    _, factory, ids = roster
    with factory() as db:
        return monthly_report(db, ids["ministry"], month, "coordinator", ids["coordinator"])


def history(roster, **kwargs):
    _, factory, ids = roster
    with factory() as db:
        return audit_history(db, ids["ministry"], "coordinator", ids["coordinator"], **kwargs)


def test_report_includes_zero_load_volunteers_and_does_not_infer_attendance(roster):
    engine, factory, _ = roster
    members, slots = prepare(roster, month=MONTH, days=(1, 2), members=3)
    engine.assign(members[0], slots[0])
    engine.assign(members[0], slots[1])
    result = report(roster)
    assert [(r.volunteer_id, r.distinct_days, r.completed_tasks) for r in result.volunteers] == [
        (members[0], 2, 0),
        (members[1], 0, 0),
        (members[2], 0, 0),
    ]
    assert result.needed == result.occupied == 2 and not result.vacancies
    assert result.balance_spread == 2 and result.balance_mean == pytest.approx(2 / 3)
    with factory() as db:
        assert not db.scalars(select(AuditEvent)).all()


def test_cancelled_and_absent_assignments_do_not_hide_vacancies(roster):
    engine, factory, _ = roster
    members, slots = prepare(roster, month=MONTH, days=(1, 2, 3), members=2)
    assignments = [engine.assign(members[0], slot) for slot in slots[:2]]
    with factory.begin() as db:
        db.get(Assignment, assignments[0]).status = "absent"
        db.get(Assignment, assignments[1]).status = "cancelled"
    result = report(roster)
    assert result.needed == 3 and result.occupied == 0
    assert sum(v.positions for v in result.vacancies) == 3
    assert result.volunteers[0].absences == 1 and result.volunteers[0].distinct_days == 0


def test_completed_attendance_and_pending_reservations_are_reported_separately(roster):
    engine, factory, _ = roster
    members, slots = prepare(roster, month=MONTH, days=(1, 2), members=2)
    first = engine.assign(members[0], slots[0])
    second = engine.assign(members[1], slots[1])
    with factory.begin() as db:
        db.get(Assignment, first).status = "completed"
        db.get(Assignment, second).status = "pending_confirmation"
    result = report(roster)
    assert result.volunteers[0].completed_tasks == 1
    assert result.volunteers[1].pending_tasks == 1 and result.volunteers[1].distinct_days == 0
    assert result.pending_confirmations == 1 and result.occupied == 2


def test_pending_replacement_is_visible_and_original_still_occupies_capacity(roster):
    engine, factory, _ = roster
    members, slots = prepare(roster, month=MONTH, members=2)
    original = engine.assign(members[0], slots[0])
    with factory.begin() as db:
        request_replacement(db, members[0], original)
    result = report(roster)
    assert dict(result.replacements) == {"pending": 1}
    assert result.occupied == 1 and result.volunteers[0].replacement_tasks == 1


def test_report_month_is_civil_service_month_and_excludes_other_months(roster):
    engine, _, _ = roster
    members, slots = prepare(roster, month=MONTH, days=(31,), members=2)
    engine.assign(members[0], slots[0])
    assert report(roster).needed == 1 and report(roster, MONTH + timedelta(days=32)).needed == 0


def test_reports_execute_only_selects(roster):
    _, factory, _ = roster
    prepare(roster, month=MONTH)
    statements = []

    def record(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(factory.kw["bind"], "before_cursor_execute", record)
    try:
        report(roster)
        history(roster)
    finally:
        event.remove(factory.kw["bind"], "before_cursor_execute", record)
    assert statements and all(s.lstrip().upper().startswith("SELECT") for s in statements)


def test_revoked_access_and_volunteer_actor_cannot_read_reports(roster):
    _, factory, ids = roster
    with factory() as db, pytest.raises(NotAuthorized):
        monthly_report(db, ids["ministry"], MONTH, "volunteer", 1)
    with factory.begin() as db:
        db.delete(db.scalar(select(CoordinatorMinistry)))
    with pytest.raises(NotAuthorized):
        report(roster)
    with pytest.raises(NotAuthorized):
        history(roster)


def test_coordinator_audit_is_scoped_and_pagination_has_no_duplicates(roster):
    _, factory, ids = roster
    with factory.begin() as db:
        other = Ministry(church_id=ids["church"], name="Outro ministério")
        db.add(other)
        db.flush()
        for n in range(5):
            audit(db, ids["church"], "system", 0, f"own_{n}", "ministry", ids["ministry"])
            audit(db, ids["church"], "system", 0, "other", "ministry", other.id)
        audit(db, ids["church"], "system", 0, "legacy_unscoped", "volunteer", 999)
    page = history(roster, limit=2)
    next_page = history(roster, before_id=page.next_before_id, limit=2)
    last_page = history(roster, before_id=next_page.next_before_id, limit=2)
    rows = page.events + next_page.events + last_page.events
    assert len(rows) == len({r.id for r in rows}) == 5
    assert all(r.action.startswith("own_") for r in rows)
    assert last_page.next_before_id is None


def test_audit_scope_is_recorded_with_domain_changes(roster):
    engine, factory, ids = roster
    members, slots = prepare(roster, month=MONTH, members=2)
    assignment = engine.assign(members[0], slots[0])
    with factory.begin() as db:
        audit(db, ids["church"], "volunteer", members[0], "registered", "assignment", assignment)
    page = history(roster)
    assert page.events[0].action == "registered"
    with factory() as db:
        assert db.scalar(select(AuditEvent.ministry_id)) == ids["ministry"]


@pytest.mark.parametrize("before_id,limit", [(0, 20), (-1, 20), (None, 0), (None, 101)])
def test_invalid_audit_pagination_is_rejected(roster, before_id, limit):
    with pytest.raises(RosterError):
        history(roster, before_id=before_id, limit=limit)


def test_real_chat_exposes_reports_only_to_authorized_coordination(roster):
    _, factory, _ = roster
    prepare(roster, month=MONTH)
    simulate(COORD, "Oi", factory=factory)
    assert "RELATÓRIO" in bodies(simulate(COORD, "RELATORIO 2027-01", factory=factory))
    assert "AUDITORIA" in bodies(simulate(COORD, "AUDITORIA", factory=factory))
    assert "RELATÓRIO" not in bodies(simulate(FIRST, "RELATORIO 2027-01", factory=factory))


def test_declines_include_normal_and_exceptional_invitations_separately_from_reservations(roster):
    engine, factory, ids = roster
    members, slots = prepare(roster, month=MONTH, days=(1, 2, 3), members=2)
    for slot in slots[:2]:
        engine.assign(members[0], slot)
    exceptional = engine.request_third_day(
        ids["coordinator"], members[0], slots[2], "Cobertura pontual"
    )
    engine.respond_third_day(members[0], exceptional, False)
    with factory.begin() as db:
        engine._lock_member_slot(db, members[1], slots[2])
        offer = create_invitation(
            db,
            Settings(_env_file=None),
            kind="distribution",
            source_key="report-decline",
            volunteer_id=members[1],
            slot_id=slots[2],
            reason="Cobertura",
        )
        respond_offer(db, engine, members[1], offer.id, False, Settings(_env_file=None))
    reservation = engine.assign(members[1], slots[2])
    with factory.begin() as db:
        db.get(Assignment, reservation).status = "declined"
    result = report(roster)
    assert result.declined_invitations == 2 and result.declined_reservations == 1


def test_fulfilled_replacement_does_not_count_original_twice(roster):
    engine, factory, _ = roster
    members, slots = prepare(roster, month=MONTH)
    original = engine.assign(members[0], slots[0])
    with factory.begin() as db:
        request_id = request_replacement(db, members[0], original)
        manual_replace(db, engine, request_id, members[1])
    result = report(roster)
    assert dict(result.replacements) == {"fulfilled": 1}
    assert result.occupied == 1
    assert [r.distinct_days for r in result.volunteers] == [0, 1]


def test_inactive_historical_member_is_retained_but_excluded_from_balance_population(roster):
    engine, factory, _ = roster
    members, slots = prepare(roster, month=MONTH)
    engine.assign(members[0], slots[0])
    with factory.begin() as db:
        db.get(Volunteer, members[0]).active = False
    result = report(roster)
    assert result.volunteers[0].distinct_days == 1 and not result.volunteers[0].eligible_population
    assert result.balance_population == 1 and result.balance_mean == 0


def test_admin_reads_unscoped_church_history_but_never_another_church(roster):
    _, factory, ids = roster
    with factory.begin() as db:
        admin = GeneralAdmin(church_id=ids["church"], phone_e164="+15550109999")
        other = Church(name="Outra igreja")
        db.add_all([admin, other])
        db.flush()
        admin_id = admin.id
        audit(db, ids["church"], "system", 0, "own_legacy", "volunteer", 999)
        audit(db, other.id, "system", 0, "private_other_church", "volunteer", 999)
    with factory() as db:
        page = audit_history(db, ids["ministry"], "admin", admin_id)
        assert [r.action for r in page.events] == ["own_legacy"]
    assert not history(roster).events


def test_cross_church_or_inconsistent_audit_scope_is_rejected(roster):
    _, factory, ids = roster
    with factory.begin() as db:
        other = Church(name="Outra igreja")
        db.add(other)
        db.flush()
        other_id = other.id
    with factory.begin() as db, pytest.raises(RosterError, match="audit ministry"):
        audit(db, other_id, "system", 0, "invalid", "ministry", ids["ministry"])


def test_cursor_remains_stable_when_new_events_arrive_between_pages(roster):
    _, factory, ids = roster
    with factory.begin() as db:
        for n in range(3):
            audit(db, ids["church"], "system", 0, f"old_{n}", "ministry", ids["ministry"])
    first = history(roster, limit=2)
    with factory.begin() as db:
        audit(db, ids["church"], "system", 0, "new", "ministry", ids["ministry"])
    second = history(roster, limit=2, before_id=first.next_before_id)
    assert len(first.events + second.events) == 3
    assert len({r.id for r in first.events + second.events}) == 3
    assert all(r.action.startswith("old_") for r in first.events + second.events)


def test_other_ministry_participations_are_excluded_and_its_report_is_forbidden(roster):
    engine, factory, ids = roster
    members, slots = prepare(roster, month=MONTH)
    engine.assign(members[0], slots[0])
    with factory.begin() as db:
        other = Ministry(church_id=ids["church"], name="Outro ministério")
        db.add(other)
        db.flush()
        role = Role(ministry_id=other.id, name="Função")
        db.add(role)
        db.flush()
        db.add(VolunteerRole(volunteer_id=members[0], role_id=role.id))
        other_id, role_id = other.id, role.id
        schedule_for(db, other.id, MONTH, create=True).state = "open"
    _, extra = make_service(engine, factory, other_id, MONTH + timedelta(days=1), {role_id: 1})
    engine.assign(members[0], extra[role_id])
    result = report(roster)
    assert result.needed == 1 and result.volunteers[0].distinct_days == 1
    with factory() as db, pytest.raises(NotAuthorized):
        monthly_report(db, other_id, MONTH, "coordinator", ids["coordinator"])
