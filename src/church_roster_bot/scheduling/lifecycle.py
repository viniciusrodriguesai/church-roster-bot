"""Explicit schedule and assignment transitions shared by chat and future automation."""

import hashlib
import json
from datetime import date, datetime
from difflib import ndiff

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from church_roster_bot.audit.models import AuditEvent, ScheduleVersion
from church_roster_bot.audit.scope import entity_ministry
from church_roster_bot.core.time import utc_now
from church_roster_bot.members.models import Volunteer
from church_roster_bot.ministries.models import Ministry, Role
from church_roster_bot.scheduling.engine import OCCUPYING, RosterEngine
from church_roster_bot.scheduling.errors import NoCapacity, NotFound, RosterError
from church_roster_bot.scheduling.models import (
    Assignment,
    MonthlySchedule,
    ReplacementRequest,
    ThirdDayRequest,
)
from church_roster_bot.services.models import Slot, WorshipService
from church_roster_bot.services.timing import service_start

SCHEDULE_TRANSITIONS = {
    "draft": {"open", "cancelled"},
    "open": {"under_review", "published", "closed", "cancelled"},
    "under_review": {"open", "published", "closed", "cancelled"},
    "published": {"closed", "cancelled"},
    "closed": set(),
    "cancelled": set(),
}
ASSIGNMENT_TRANSITIONS = {
    "pending_confirmation": {"confirmed", "declined", "cancelled"},
    "confirmed": {"replacement_requested", "cancelled", "completed", "absent"},
    "replacement_requested": {"replaced", "confirmed", "cancelled"},
    "replaced": set(),
    "cancelled": set(),
    "completed": set(),
    "absent": set(),
    "declined": set(),
}
WEEKDAYS = ("Segunda", "Terça", "Quarta", "Quinta", "Sexta", "Sábado", "Domingo")


def schedule_for(db: Session, ministry_id: int, day: date, *, create: bool = False):
    row = db.scalar(
        select(MonthlySchedule).where(
            MonthlySchedule.ministry_id == ministry_id,
            MonthlySchedule.year == day.year,
            MonthlySchedule.month == day.month,
        )
    )
    if row is None and create:
        row = MonthlySchedule(ministry_id=ministry_id, year=day.year, month=day.month)
        db.add(row)
        db.flush()
    return row


def transition_schedule(schedule: MonthlySchedule, target: str) -> None:
    if target not in SCHEDULE_TRANSITIONS.get(schedule.state, set()):
        raise RosterError(f"invalid schedule transition: {schedule.state} -> {target}")
    schedule.state = target


def transition_assignment(assignment: Assignment, target: str) -> None:
    if assignment.status == "pending_confirmation" and target == "confirmed":
        raise RosterError("confirm reservations through RosterEngine.confirm_reservation")
    if target not in ASSIGNMENT_TRANSITIONS.get(assignment.status, set()):
        raise RosterError(f"invalid assignment transition: {assignment.status} -> {target}")
    assignment.status = target


def slot_state(db: Session, slot: Slot, *, schedule_state: str = "open") -> str:
    if schedule_state == "cancelled":
        return "CANCELLED"
    statuses = db.scalars(select(Assignment.status).where(Assignment.slot_id == slot.id)).all()
    if "replacement_requested" in statuses:
        return "REPLACEMENT_PENDING"
    if statuses.count("completed") >= slot.capacity:
        return "COMPLETED"
    occupied = sum(status in OCCUPYING for status in statuses)
    if schedule_state == "closed" and occupied < slot.capacity:
        return "UNCOVERED"
    if occupied >= slot.capacity:
        return "FILLED" if "pending_confirmation" not in statuses else "RESERVED"
    return "AVAILABLE" if occupied == 0 else "RESERVED"


def set_slot_capacity(db: Session, slot_id: int, capacity: int) -> None:
    if capacity <= 0:
        raise RosterError("capacity must be positive")
    slot = db.get(Slot, slot_id, with_for_update=True)
    if slot is None:
        raise NotFound("slot not found")
    occupied = (
        db.scalar(
            select(func.count(Assignment.id)).where(
                Assignment.slot_id == slot_id, Assignment.status.in_(OCCUPYING)
            )
        )
        or 0
    )
    if capacity < occupied:
        raise NoCapacity(f"capacity cannot be lower than {occupied} active assignments")
    slot.capacity = capacity


def request_replacement(
    db: Session,
    volunteer_id: int,
    assignment_id: int,
    *,
    now: datetime | None = None,
) -> int:
    assignment = db.get(Assignment, assignment_id, with_for_update=True)
    if assignment is None or assignment.volunteer_id != volunteer_id:
        raise NotFound("assignment not found for this volunteer")
    slot = db.get(Slot, assignment.slot_id)
    service = db.get(WorshipService, slot.service_id)
    if service_start(db, service) <= utc_now(now):
        raise RosterError("past service cannot be replaced")
    transition_assignment(assignment, "replacement_requested")
    request = ReplacementRequest(assignment_id=assignment_id)
    db.add(request)
    db.flush()
    return request.id


def lock_replacement_members(
    db: Session, request_id: int, replacement_volunteer_id: int
) -> tuple[ReplacementRequest, Assignment]:
    """Lock both recipients by ID before any replacement slot or offer lock."""
    request = db.get(ReplacementRequest, request_id)
    if request is None:
        raise NotFound("replacement request not found")
    original = db.get(Assignment, request.assignment_id)
    list(
        db.scalars(
            select(Volunteer)
            .where(Volunteer.id.in_({original.volunteer_id, replacement_volunteer_id}))
            .order_by(Volunteer.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    )
    return request, original


def manual_replace(
    db: Session,
    roster: RosterEngine,
    request_id: int,
    replacement_volunteer_id: int,
    *,
    now: datetime | None = None,
) -> int:
    with db.begin_nested():
        return _manual_replace(db, roster, request_id, replacement_volunteer_id, now=now)


def _manual_replace(
    db: Session,
    roster: RosterEngine,
    request_id: int,
    replacement_volunteer_id: int,
    *,
    now: datetime | None = None,
) -> int:
    request, original = lock_replacement_members(db, request_id, replacement_volunteer_id)
    if request.status != "pending":
        raise NotFound("pending replacement not found")
    # Same order as acceptance: both members, slot, offer (if any), request, original.
    member, slot = roster._lock_member_slot(db, replacement_volunteer_id, original.slot_id)
    request = db.get(ReplacementRequest, request_id, with_for_update=True, populate_existing=True)
    if request.status != "pending":
        raise NotFound("pending replacement not found")
    original = db.get(
        Assignment, request.assignment_id, with_for_update=True, populate_existing=True
    )
    if original.status != "replacement_requested":
        raise RosterError("original assignment is not awaiting replacement")
    if original.volunteer_id == replacement_volunteer_id:
        raise RosterError("replacement must be a different volunteer")
    old_slot = db.get(Slot, original.slot_id)
    old_service = db.get(WorshipService, old_slot.service_id)
    if service_start(db, old_service) <= utc_now(now):
        raise RosterError("past service cannot be replaced")
    schedule = schedule_for(db, old_service.ministry_id, old_service.service_date)
    if schedule and schedule.state in {"closed", "cancelled"}:
        raise RosterError("closed schedule cannot be changed")
    transition_assignment(original, "replaced")
    replacement_id = roster._assign_locked(db, member, slot, allow_published=True)
    request.status = "fulfilled"
    request.replacement_assignment_id = replacement_id
    if schedule and schedule.state == "published":
        publish_schedule(db, old_service.ministry_id, old_service.service_date)
    return replacement_id


def format_schedule(db: Session, ministry_id: int, day: date) -> str:
    ministry = db.get(Ministry, ministry_id)
    if ministry is None:
        raise NotFound("ministry not found")
    start = day.replace(day=1)
    end = date(day.year + (day.month == 12), day.month % 12 + 1, 1)
    services = db.scalars(
        select(WorshipService)
        .where(
            WorshipService.ministry_id == ministry_id,
            WorshipService.service_date >= start,
            WorshipService.service_date < end,
        )
        .order_by(WorshipService.service_date, WorshipService.start_time)
    ).all()
    lines = [f"ESCALA DO {ministry.name.upper()} — {day:%m/%Y}"]
    for service in services:
        lines.append(
            f"\n{WEEKDAYS[service.service_date.weekday()]} — "
            f"{service.service_date:%d/%m} {service.start_time:%H:%M}"
        )
        slots = db.scalars(select(Slot).where(Slot.service_id == service.id)).all()
        for slot in slots:
            role = db.get(Role, slot.role_id)
            assigned = db.scalars(
                select(Volunteer.name)
                .join(Assignment, Assignment.volunteer_id == Volunteer.id)
                .where(
                    Assignment.slot_id == slot.id,
                    Assignment.status.in_(("confirmed", "replacement_requested", "completed")),
                )
                .order_by(Volunteer.name)
            ).all()
            names = list(assigned) + ["VAGA EM ABERTO"] * max(0, slot.capacity - len(assigned))
            lines.append(f"{role.name}: {', '.join(names)}")
    if not services:
        lines.append("Nenhum culto cadastrado.")
    return "\n".join(lines)


def invitation_text(db: Session, invitation: ThirdDayRequest) -> str:
    slot = db.get(Slot, invitation.slot_id)
    service = db.get(WorshipService, slot.service_id)
    role = db.get(Role, slot.role_id)
    return (
        f"Convite excepcional #{invitation.id}: {service.service_date:%d/%m/%Y} "
        f"às {service.start_time:%H:%M} — {role.name}.\n"
        f"Justificativa: {invitation.justification}\n"
        f"Envie ACEITAR {invitation.id} ou RECUSAR {invitation.id}."
    )


def publish_schedule(db: Session, ministry_id: int, day: date) -> ScheduleVersion:
    schedule = schedule_for(db, ministry_id, day, create=True)
    schedule = db.scalar(
        select(MonthlySchedule)
        .where(MonthlySchedule.id == schedule.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if schedule.state not in {"open", "under_review", "published"}:
        raise RosterError("schedule is not ready for publication")
    content = format_schedule(db, ministry_id, day)
    start = day.replace(day=1)
    end = date(day.year + (day.month == 12), day.month % 12 + 1, 1)
    rows = db.execute(
        select(
            WorshipService.id,
            Slot.id,
            Slot.capacity,
            Assignment.id,
            Assignment.volunteer_id,
            Assignment.status,
        )
        .join(Slot, Slot.service_id == WorshipService.id)
        .outerjoin(Assignment, Assignment.slot_id == Slot.id)
        .where(
            WorshipService.ministry_id == ministry_id,
            WorshipService.service_date >= start,
            WorshipService.service_date < end,
        )
        .order_by(WorshipService.id, Slot.id, Assignment.id)
    ).all()
    fingerprint = hashlib.sha256(json.dumps([tuple(row) for row in rows]).encode()).hexdigest()
    previous = db.scalar(
        select(ScheduleVersion)
        .where(ScheduleVersion.schedule_id == schedule.id)
        .order_by(ScheduleVersion.version.desc())
        .limit(1)
    )
    if previous and previous.fingerprint == fingerprint and previous.content == content:
        return previous
    if schedule.state != "published":
        transition_schedule(schedule, "published")
    schedule.version += 1
    update_message = None
    if previous:
        changes = [
            line
            for line in ndiff(previous.content.splitlines(), content.splitlines())
            if line.startswith(("+ ", "- "))
        ]
        update_message = (
            f"ATUALIZAÇÃO DA ESCALA — {day:%m/%Y} — versão {schedule.version}\n"
            + ("\n".join(changes) or "Responsável alterado; consulte a escala completa.")
        )[:6000]
    version = ScheduleVersion(
        schedule_id=schedule.id,
        version=schedule.version,
        content=content,
        fingerprint=fingerprint,
        update_message=update_message,
    )
    db.add(version)
    db.flush()
    return version


def audit(
    db: Session,
    church_id: int,
    actor_type: str,
    actor_id: int,
    action: str,
    entity_type: str,
    entity_id: int,
    details: dict | None = None,
    *,
    ministry_id: int | None = None,
) -> None:
    derived = entity_ministry(db, entity_type, entity_id)
    if ministry_id is None:
        ministry_id = derived
    if ministry_id is not None:
        ministry = db.get(Ministry, ministry_id)
        if (
            ministry is None
            or ministry.church_id != church_id
            or (derived is not None and derived != ministry_id)
        ):
            raise RosterError("audit ministry does not match its church and entity")
    db.add(
        AuditEvent(
            church_id=church_id,
            ministry_id=ministry_id,
            actor_type=actor_type,
            actor_id=actor_id,
            action=action,
            entity_type=entity_type,
            entity_id=entity_id,
            details=details or {},
        )
    )
