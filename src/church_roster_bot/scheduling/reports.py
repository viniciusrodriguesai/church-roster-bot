"""Authorized, read-only monthly statistics and paginated audit snapshots."""

import json
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import date, datetime
from statistics import fmean, pstdev

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from church_roster_bot.administration.service import authorize_actor
from church_roster_bot.audit.models import AuditEvent
from church_roster_bot.core.time import database_utc
from church_roster_bot.members.models import Volunteer, VolunteerRole
from church_roster_bot.ministries.models import Role
from church_roster_bot.scheduling.engine import OCCUPYING, month_bounds
from church_roster_bot.scheduling.errors import RosterError
from church_roster_bot.scheduling.models import (
    Assignment,
    AutomationInvitation,
    ReplacementRequest,
    ThirdDayRequest,
)
from church_roster_bot.services.models import Slot, WorshipService


@dataclass(frozen=True)
class VolunteerFrequency:
    volunteer_id: int
    name: str
    eligible_population: bool
    distinct_days: int
    confirmed_tasks: int
    completed_tasks: int
    pending_tasks: int
    replacement_tasks: int
    absences: int


@dataclass(frozen=True)
class ReportVacancy:
    slot_id: int
    service_date: date
    role: str
    positions: int


@dataclass(frozen=True)
class MonthlyReport:
    ministry_name: str
    month: date
    volunteers: tuple[VolunteerFrequency, ...]
    needed: int
    occupied: int
    pending_confirmations: int
    vacancies: tuple[ReportVacancy, ...]
    replacements: tuple[tuple[str, int], ...]
    declined_invitations: int
    declined_reservations: int
    balance_population: int
    balance_mean: float
    balance_spread: int
    balance_stddev: float


def monthly_report(
    db: Session, ministry_id: int, month: date, actor_type: str, actor_id: int
) -> MonthlyReport:
    ministry = authorize_actor(db, ministry_id, actor_type, actor_id)
    start, end = month_bounds(month)
    slots = db.execute(
        select(Slot, WorshipService, Role)
        .select_from(Slot)
        .join(WorshipService, WorshipService.id == Slot.service_id)
        .join(Role, Role.id == Slot.role_id)
        .where(
            WorshipService.ministry_id == ministry_id,
            WorshipService.service_date >= start,
            WorshipService.service_date < end,
        )
        .order_by(WorshipService.service_date, WorshipService.start_time, Slot.id)
    ).all()
    slot_ids = [slot.id for slot, _, _ in slots]
    assignments = db.scalars(select(Assignment).where(Assignment.slot_id.in_(slot_ids))).all()
    eligible_ids = set(
        db.scalars(
            select(Volunteer.id)
            .join(VolunteerRole, VolunteerRole.volunteer_id == Volunteer.id)
            .join(Role, Role.id == VolunteerRole.role_id)
            .where(
                Role.ministry_id == ministry_id,
                Volunteer.church_id == ministry.church_id,
                Volunteer.active.is_(True),
            )
            .distinct()
        )
    )
    members = db.scalars(
        select(Volunteer)
        .where(
            Volunteer.church_id == ministry.church_id,
            or_(
                Volunteer.id.in_(eligible_ids),
                Volunteer.id.in_({a.volunteer_id for a in assignments}),
            ),
        )
        .order_by(Volunteer.id)
    ).all()
    dates = {slot.id: service.service_date for slot, service, _ in slots}
    committed = {"confirmed", "completed", "replacement_requested"}
    by_volunteer: dict[int, list[Assignment]] = defaultdict(list)
    for assignment in assignments:
        by_volunteer[assignment.volunteer_id].append(assignment)
    volunteers = []
    for member in members:
        own = by_volunteer[member.id]
        counts = Counter(a.status for a in own)
        volunteers.append(
            VolunteerFrequency(
                member.id,
                member.name,
                member.id in eligible_ids,
                len({dates[a.slot_id] for a in own if a.status in committed}),
                counts["confirmed"],
                counts["completed"],
                counts["pending_confirmation"],
                counts["replacement_requested"],
                counts["absent"],
            )
        )
    occupied = Counter(a.slot_id for a in assignments if a.status in OCCUPYING)
    vacancies = tuple(
        ReportVacancy(slot.id, service.service_date, role.name, slot.capacity - occupied[slot.id])
        for slot, service, role in slots
        if occupied[slot.id] < slot.capacity
    )
    replacements = Counter(
        db.scalars(
            select(ReplacementRequest.status)
            .join(Assignment, Assignment.id == ReplacementRequest.assignment_id)
            .where(Assignment.slot_id.in_(slot_ids))
        )
    )
    declined = sum(
        db.scalar(
            select(func.count(model.id)).where(
                model.slot_id.in_(slot_ids), model.status == "declined"
            )
        )
        or 0
        for model in (AutomationInvitation, ThirdDayRequest)
    )
    balance = [row.distinct_days for row in volunteers if row.eligible_population]
    return MonthlyReport(
        ministry.name,
        start,
        tuple(volunteers),
        sum(slot.capacity for slot, _, _ in slots),
        sum(occupied.values()),
        sum(a.status == "pending_confirmation" for a in assignments),
        vacancies,
        tuple(sorted(replacements.items())),
        declined,
        sum(a.status == "declined" for a in assignments),
        len(balance),
        fmean(balance) if balance else 0.0,
        max(balance) - min(balance) if balance else 0,
        pstdev(balance) if balance else 0.0,
    )


@dataclass(frozen=True)
class AuditSnapshot:
    id: int
    created_at: datetime
    actor_type: str
    actor_id: int
    action: str
    entity_type: str
    entity_id: int
    details: str


@dataclass(frozen=True)
class AuditPage:
    events: tuple[AuditSnapshot, ...]
    next_before_id: int | None


def audit_history(
    db: Session,
    ministry_id: int,
    actor_type: str,
    actor_id: int,
    *,
    before_id: int | None = None,
    limit: int = 20,
) -> AuditPage:
    ministry = authorize_actor(db, ministry_id, actor_type, actor_id)
    if not 1 <= limit <= 100 or (before_id is not None and before_id < 1):
        raise RosterError("use a positive audit cursor and limit 1..100")
    query = select(AuditEvent).where(AuditEvent.church_id == ministry.church_id)
    if actor_type != "admin":
        query = query.where(AuditEvent.ministry_id == ministry_id)
    if before_id is not None:
        query = query.where(AuditEvent.id < before_id)
    rows = db.scalars(query.order_by(AuditEvent.id.desc()).limit(limit + 1)).all()
    events = tuple(
        AuditSnapshot(
            row.id,
            database_utc(row.created_at),
            row.actor_type,
            row.actor_id,
            row.action,
            row.entity_type,
            row.entity_id,
            json.dumps(row.details, sort_keys=True, ensure_ascii=False),
        )
        for row in rows[:limit]
    )
    return AuditPage(events, events[-1].id if len(rows) > limit else None)
