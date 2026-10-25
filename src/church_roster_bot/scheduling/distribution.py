"""Deterministic, advisory roster proposals; no assignments or messages are written."""

import logging
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from church_roster_bot.administration.service import require_ministry
from church_roster_bot.members.models import Coordinator, GeneralAdmin, Volunteer, VolunteerRole
from church_roster_bot.ministries.models import Church, Role, RoleConflict
from church_roster_bot.scheduling.engine import (
    OCCUPYING,
    AssignmentIntent,
    RosterEngine,
    month_bounds,
)
from church_roster_bot.scheduling.errors import NotAuthorized, RosterError
from church_roster_bot.scheduling.lifecycle import schedule_for
from church_roster_bot.scheduling.models import Assignment
from church_roster_bot.services.models import Slot, WorshipService

logger = logging.getLogger(__name__)
POLICY_VERSION = "balanced-v1"


@dataclass(frozen=True)
class CandidateScore:
    monthly_days: int
    monthly_assignments: int
    historical_days: int
    last_served: date | None

    def rank(self, volunteer_id: int) -> tuple[int, int, int, date, int]:
        return (
            self.monthly_days,
            self.monthly_assignments,
            self.historical_days,
            self.last_served or date.min,
            volunteer_id,
        )

    @property
    def explanation(self) -> str:
        last_served = self.last_served.isoformat() if self.last_served else "nenhuma"
        return (
            f"carga projetada antes da escolha: {self.monthly_days} dia(s), "
            f"{self.monthly_assignments} tarefa(s) no mês; "
            f"histórico: {self.historical_days} dia(s); "
            f"última participação: {last_served}"
        )


@dataclass(frozen=True)
class SuggestedAssignment:
    intent: AssignmentIntent
    score: CandidateScore


@dataclass(frozen=True)
class UnfilledSlot:
    slot_id: int
    positions: int
    # Aggregate domain rejection codes, with no personal data.
    rejections: tuple[tuple[str, int], ...]


@dataclass(frozen=True)
class DistributionProposal:
    ministry_id: int
    month: date
    generated_at: datetime
    policy_version: str
    suggestions: tuple[SuggestedAssignment, ...]
    unfilled: tuple[UnfilledSlot, ...]


class DistributionPlanner:
    def __init__(self, roster: RosterEngine):
        self.roster = roster

    def propose(
        self,
        db: Session,
        ministry_id: int,
        month: date,
        *,
        actor_type: Literal["admin", "coordinator"],
        actor_id: int,
        now: datetime | None = None,
    ) -> DistributionProposal:
        """Calculate advice for an authorized actor; approval is a separate boundary.

        Current database rules are always checked by RosterEngine. Advisory
        choices add only in-memory load. Callers must not treat this result as
        a reservation or confirmation, even when nothing remains unfilled.
        """
        instant = now if now is not None else datetime.now(UTC)
        if instant.utcoffset() is None:
            raise RosterError("proposal time must include a timezone")
        with db.no_autoflush:
            return self._propose(db, ministry_id, month, actor_type, actor_id, instant)

    def _propose(
        self,
        db: Session,
        ministry_id: int,
        month: date,
        actor_type: str,
        actor_id: int,
        instant: datetime,
    ) -> DistributionProposal:
        actor_class = {"admin": GeneralAdmin, "coordinator": Coordinator}.get(actor_type)
        if actor_class is None:
            raise NotAuthorized("only administrators and coordinators may propose rosters")
        actor = db.scalar(
            select(actor_class).where(actor_class.id == actor_id, actor_class.active.is_(True))
        )
        if actor is None:
            raise NotAuthorized("active actor not found")
        ministry = require_ministry(
            db,
            ministry_id,
            actor if actor_type == "admin" else None,
            actor if actor_type == "coordinator" else None,
        )
        start, end = month_bounds(month)
        schedule = schedule_for(db, ministry_id, start)
        if schedule is None or schedule.state != "open":
            raise RosterError("proposals require an open monthly schedule")
        church = db.get(Church, ministry.church_id)
        local_now = instant.astimezone(ZoneInfo(church.timezone))
        slots = {
            slot.id: (slot, service)
            for slot, service in db.execute(
                select(Slot, WorshipService)
                .join(WorshipService, WorshipService.id == Slot.service_id)
                .where(
                    WorshipService.ministry_id == ministry_id,
                    WorshipService.service_date >= start,
                    WorshipService.service_date < end,
                )
                .order_by(WorshipService.service_date, WorshipService.start_time, Slot.id)
            )
            if datetime.combine(service.service_date, service.start_time)
            > local_now.replace(tzinfo=None)
        }
        members = tuple(
            db.scalars(
                select(Volunteer.id)
                .join(VolunteerRole, VolunteerRole.volunteer_id == Volunteer.id)
                .join(Role, Role.id == VolunteerRole.role_id)
                .where(
                    Volunteer.church_id == ministry.church_id,
                    Volunteer.active.is_(True),
                    Role.ministry_id == ministry_id,
                    Role.active.is_(True),
                )
                .distinct()
                .order_by(Volunteer.id)
            )
        )
        monthly_days: dict[int, set[date]] = defaultdict(set)
        monthly_tasks: Counter[int] = Counter()
        historical_days: dict[int, set[date]] = defaultdict(set)
        occupancy: Counter[int] = Counter()
        for member_id, slot_id, status, day in db.execute(
            select(
                Assignment.volunteer_id,
                Assignment.slot_id,
                Assignment.status,
                WorshipService.service_date,
            )
            .join(Slot, Slot.id == Assignment.slot_id)
            .join(WorshipService, WorshipService.id == Slot.service_id)
            .where(
                or_(Assignment.volunteer_id.in_(members), Slot.id.in_(slots)),
                WorshipService.service_date >= start - timedelta(days=90),
                WorshipService.service_date < end,
            )
        ):
            if start <= day and status in OCCUPYING:
                if member_id in members:
                    monthly_days[member_id].add(day)
                    monthly_tasks[member_id] += 1
                if slot_id in slots:
                    occupancy[slot_id] += 1
            elif (
                member_id in members
                and day < min(start, local_now.date())
                and status in {"confirmed", "completed"}
            ):
                historical_days[member_id].add(day)
        remaining = {
            slot_id: slot.capacity - occupancy[slot_id]
            for slot_id, (slot, _) in slots.items()
            if slot.capacity > occupancy[slot_id]
        }
        # Query current eligibility once per member/slot pair. Recomputing every
        # SQL rule for every remaining position makes even small months expensive.
        base_candidates: dict[int, list[int]] = {}
        base_rejections: dict[int, Counter[str]] = {}
        for slot_id in remaining:
            base_candidates[slot_id], base_rejections[slot_id] = [], Counter()
            for member_id in members:
                try:
                    self.roster.check_assignment(db, member_id, slot_id)
                except RosterError as exc:
                    base_rejections[slot_id][type(exc).__name__] += 1
                else:
                    base_candidates[slot_id].append(member_id)
        conflict_pairs = {
            frozenset((pair.role_a_id, pair.role_b_id)) for pair in db.scalars(select(RoleConflict))
        }
        projected_roles: dict[tuple[int, int], set[int]] = defaultdict(set)
        suggestions: list[SuggestedAssignment] = []
        projected: list[AssignmentIntent] = []
        unfilled: list[UnfilledSlot] = []
        while remaining:
            candidates: dict[int, list[int]] = {}
            rejections: dict[int, Counter[str]] = {}
            for slot_id in remaining:
                candidates[slot_id] = []
                rejections[slot_id] = base_rejections[slot_id].copy()
                slot, service = slots[slot_id]
                for member_id in base_candidates[slot_id]:
                    roles = projected_roles[member_id, service.id]
                    if slot.role_id in roles:
                        rejections[slot_id]["RosterError"] += 1
                    elif any(frozenset((slot.role_id, role)) in conflict_pairs for role in roles):
                        rejections[slot_id]["RoleConflictError"] += 1
                    elif (
                        service.service_date not in monthly_days[member_id]
                        and len(monthly_days[member_id]) >= 2
                    ):
                        rejections[slot_id]["MonthlyLimit"] += 1
                    else:
                        candidates[slot_id].append(member_id)
            slot_id = min(
                remaining,
                key=lambda key: (
                    len(candidates[key]),
                    slots[key][1].service_date,
                    slots[key][1].start_time,
                    key,
                ),
            )
            if not candidates[slot_id]:
                unfilled.append(
                    UnfilledSlot(
                        slot_id, remaining.pop(slot_id), tuple(sorted(rejections[slot_id].items()))
                    )
                )
                continue
            scores = {
                member_id: CandidateScore(
                    len(monthly_days[member_id]),
                    monthly_tasks[member_id],
                    len(historical_days[member_id]),
                    max(historical_days[member_id], default=None),
                )
                for member_id in candidates[slot_id]
            }
            selected = min(candidates[slot_id], key=lambda key: scores[key].rank(key))
            # The in-memory filter only ranks candidates. The shared engine is
            # still the final authority for every proposed choice and projection.
            self.roster.check_assignment(db, selected, slot_id, projected=projected)
            intent = AssignmentIntent(selected, slot_id)
            suggestions.append(SuggestedAssignment(intent, scores[selected]))
            projected.append(intent)
            projected_roles[selected, slots[slot_id][1].id].add(slots[slot_id][0].role_id)
            monthly_days[selected].add(slots[slot_id][1].service_date)
            monthly_tasks[selected] += 1
            remaining[slot_id] -= 1
            if not remaining[slot_id]:
                del remaining[slot_id]
        logger.info(
            "roster_proposal_calculated",
            extra={
                "ministry_id": ministry_id,
                "month": start.isoformat(),
                "policy_version": POLICY_VERSION,
                "suggestions": len(suggestions),
                "unfilled_positions": sum(s.positions for s in unfilled),
            },
        )
        return DistributionProposal(
            ministry_id,
            start,
            instant.astimezone(UTC),
            POLICY_VERSION,
            tuple(suggestions),
            tuple(unfilled),
        )
