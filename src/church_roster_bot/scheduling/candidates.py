"""Explainable candidate ranking shared by automatic replacement and emergency offers."""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from church_roster_bot.core.time import utc_now
from church_roster_bot.members.models import Volunteer, VolunteerRole
from church_roster_bot.ministries.models import Church, Ministry
from church_roster_bot.scheduling.distribution import CandidateScore
from church_roster_bot.scheduling.engine import OCCUPYING, RosterEngine, month_bounds
from church_roster_bot.scheduling.errors import RosterError
from church_roster_bot.scheduling.models import Assignment
from church_roster_bot.services.models import Slot, WorshipService


def ranked_candidates(
    db: Session,
    roster: RosterEngine,
    slot_id: int,
    *,
    excluded: set[int] | None = None,
    replacement_request_id: int | None = None,
    now: datetime | None = None,
) -> tuple[tuple[int, CandidateScore], ...]:
    instant = utc_now(now)
    slot = db.get(Slot, slot_id)
    service = db.get(WorshipService, slot.service_id)
    ministry = db.get(Ministry, service.ministry_id)
    church = db.get(Church, ministry.church_id)
    today = instant.astimezone(ZoneInfo(church.timezone)).date()
    start, end = month_bounds(service.service_date)
    result = []
    for member in db.scalars(
        select(Volunteer)
        .join(VolunteerRole, VolunteerRole.volunteer_id == Volunteer.id)
        .where(
            VolunteerRole.role_id == slot.role_id,
            Volunteer.church_id == ministry.church_id,
            Volunteer.active.is_(True),
            Volunteer.invite_opt_in.is_(True),
        )
        .order_by(Volunteer.id)
    ):
        if member.id in (excluded or set()):
            continue
        try:
            if replacement_request_id:
                roster.check_replacement(db, member.id, replacement_request_id)
            else:
                roster.check_assignment(db, member.id, slot.id, allow_published=True)
        except RosterError:
            continue
        days = roster.monthly_days(member.id, start, db=db)
        tasks = (
            db.scalar(
                select(func.count(Assignment.id))
                .join(Slot, Slot.id == Assignment.slot_id)
                .join(WorshipService, WorshipService.id == Slot.service_id)
                .where(
                    Assignment.volunteer_id == member.id,
                    Assignment.status.in_(OCCUPYING),
                    WorshipService.service_date >= start,
                    WorshipService.service_date < end,
                )
            )
            or 0
        )
        history = set(
            db.scalars(
                select(WorshipService.service_date)
                .join(Slot, Slot.service_id == WorshipService.id)
                .join(Assignment, Assignment.slot_id == Slot.id)
                .where(
                    Assignment.volunteer_id == member.id,
                    Assignment.status.in_(("confirmed", "completed")),
                    WorshipService.service_date >= start - timedelta(days=90),
                    WorshipService.service_date < min(start, today),
                )
            )
        )
        result.append(
            (member.id, CandidateScore(days, tasks, len(history), max(history, default=None)))
        )
    return tuple(sorted(result, key=lambda item: item[1].rank(item[0])))
