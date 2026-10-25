from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager, nullcontext
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from itertools import combinations

from sqlalchemy import and_, distinct, func, or_, select
from sqlalchemy.orm import Session

from church_roster_bot.availability.models import Unavailability
from church_roster_bot.members.models import (
    Coordinator,
    CoordinatorMinistry,
    Volunteer,
    VolunteerRole,
)
from church_roster_bot.members.schemas import VolunteerCreate
from church_roster_bot.ministries.models import Ministry, Role, RoleConflict
from church_roster_bot.scheduling.errors import (
    MonthlyLimit,
    NoCapacity,
    NotAuthorized,
    NotFound,
    RoleConflictError,
    RoleMismatch,
    RosterError,
    Unavailable,
)
from church_roster_bot.scheduling.models import Assignment, MonthlySchedule, ThirdDayRequest
from church_roster_bot.services.models import Slot, WorshipService

SessionFactory = Callable[[], Session]
OCCUPYING = ("pending_confirmation", "confirmed", "replacement_requested", "completed")
COUNTED_DAYS = OCCUPYING


@dataclass(frozen=True)
class CapacityReport:
    needed_positions: int
    normal_day_capacity: int
    shortage_at_least: int
    warnings: tuple[str, ...]


@dataclass(frozen=True)
class AssignmentIntent:
    """An advisory choice; never a reservation or permission to write an assignment."""

    volunteer_id: int
    slot_id: int


def month_bounds(day: date) -> tuple[date, date]:
    start = day.replace(day=1)
    end = date(day.year + (day.month == 12), day.month % 12 + 1, 1)
    return start, end


class RosterEngine:
    """All writes use a short transaction; PostgreSQL row locks serialize competing claims."""

    def __init__(self, session_factory: SessionFactory):
        self.session_factory = session_factory

    @contextmanager
    def _transaction(self, db: Session | None):
        if db is None:
            with self.session_factory.begin() as session:
                yield session
        else:
            # A chat handler can catch domain errors and still commit its reply.
            # Roll back the failed operation before that outer transaction commits.
            with db.begin_nested():
                yield db

    def register_volunteer(
        self, church_id: int, data: VolunteerCreate, *, db: Session | None = None
    ) -> int:
        with self._transaction(db) as db:
            if not self._roles_belong_to_church(db, church_id, data.role_ids):
                raise RoleMismatch("every role must belong to this church")
            member = Volunteer(
                church_id=church_id,
                name=data.name.strip(),
                phone_e164=data.phone_e164,
                invite_opt_in=data.invite_opt_in,
            )
            db.add(member)
            db.flush()
            db.add_all(VolunteerRole(volunteer_id=member.id, role_id=rid) for rid in data.role_ids)
            return member.id

    def set_unavailability(
        self, volunteer_id: int, start: date, end: date, *, db: Session | None = None
    ) -> list[int]:
        if start > end:
            raise RosterError("start date must precede end date")
        with self._transaction(db) as db:
            member = db.get(Volunteer, volunteer_id, with_for_update=True)
            if member is None:
                raise NotFound("volunteer not found")
            db.add(Unavailability(volunteer_id=volunteer_id, start_date=start, end_date=end))
            conflicts = db.scalars(
                select(Assignment.id)
                .join(Slot, Slot.id == Assignment.slot_id)
                .join(WorshipService, WorshipService.id == Slot.service_id)
                .where(
                    Assignment.volunteer_id == volunteer_id,
                    Assignment.status.in_(COUNTED_DAYS),
                    WorshipService.service_date.between(start, end),
                )
            ).all()
            return list(conflicts)

    def create_service(
        self,
        ministry_id: int,
        service_date: date,
        start_time: time,
        capacities: Mapping[int, int],
        *,
        db: Session | None = None,
    ) -> int:
        if not capacities or any(capacity <= 0 for capacity in capacities.values()):
            raise RosterError("each configured role requires positive capacity")
        with self._transaction(db) as db:
            ministry = db.get(Ministry, ministry_id)
            if ministry is None:
                raise NotFound("ministry not found")
            valid = set(
                db.scalars(
                    select(Role.id).where(
                        Role.ministry_id == ministry_id,
                        Role.active.is_(True),
                        Role.id.in_(capacities),
                    )
                ).all()
            )
            if valid != set(capacities):
                raise RoleMismatch("slots must use active roles in the selected ministry")
            service = WorshipService(
                ministry_id=ministry_id, service_date=service_date, start_time=start_time
            )
            db.add(service)
            db.flush()
            db.add_all(
                Slot(service_id=service.id, role_id=rid, capacity=cap)
                for rid, cap in capacities.items()
            )
            return service.id

    def assign(self, volunteer_id: int, slot_id: int, *, db: Session | None = None) -> int:
        with self._transaction(db) as db:
            member, slot = self._lock_member_slot(db, volunteer_id, slot_id)
            return self._assign_locked(db, member, slot)

    def check_assignment(
        self,
        db: Session,
        volunteer_id: int,
        slot_id: int,
        *,
        projected: Sequence[AssignmentIntent] = (),
        allow_published: bool = False,
    ) -> None:
        """Read-only advisory validation; assign() must still lock and revalidate.

        Projected choices count toward capacity, conflicts and monthly days but
        never enter the session or the database. This cannot authorize a third day.
        """
        with db.no_autoflush:
            member = db.get(Volunteer, volunteer_id)
            if member is None or not member.active:
                raise NotFound("active volunteer not found")
            slot = db.get(Slot, slot_id)
            if slot is None:
                raise NotFound("slot not found")
            self._validate_assignment(
                db, member, slot, projected=projected, allow_published=allow_published
            )

    def confirm_reservation(self, assignment_id: int, *, db: Session | None = None) -> int:
        with self._transaction(db) as db:
            assignment = db.get(Assignment, assignment_id)
            if assignment is None or assignment.status != "pending_confirmation":
                raise RosterError("participation is not pending confirmation")
            member, slot = self._lock_member_slot(db, assignment.volunteer_id, assignment.slot_id)
            # Exclude this reservation from its own duplicate/capacity/day checks.
            assignment.status = "cancelled"
            db.flush()
            return self._assign_locked(db, member, slot, reservation=assignment)

    def request_third_day(
        self,
        coordinator_id: int,
        volunteer_id: int,
        slot_id: int,
        justification: str,
        *,
        db: Session | None = None,
    ) -> int:
        if not justification.strip() or len(justification.strip()) > 1000:
            raise RosterError(
                "a third-day request requires a justification of 1 to 1000 characters"
            )
        with self._transaction(db) as db:
            member, slot = self._lock_member_slot(db, volunteer_id, slot_id)
            coordinator = db.get(Coordinator, coordinator_id)
            if (
                coordinator is None
                or not coordinator.active
                or coordinator.church_id != member.church_id
            ):
                raise NotAuthorized("coordinator is not authorized for this church")
            service = db.get(WorshipService, slot.service_id)
            if (
                db.scalar(
                    select(CoordinatorMinistry.id).where(
                        CoordinatorMinistry.coordinator_id == coordinator_id,
                        CoordinatorMinistry.ministry_id == service.ministry_id,
                    )
                )
                is None
            ):
                raise NotAuthorized("coordinator does not manage this ministry")
            self._check_eligibility(db, member, slot, service)
            existing = db.scalar(
                select(ThirdDayRequest).where(
                    ThirdDayRequest.volunteer_id == volunteer_id,
                    ThirdDayRequest.slot_id == slot_id,
                    ThirdDayRequest.status == "pending",
                )
            )
            if existing:
                return existing.id
            if self._monthly_days(db, volunteer_id, service.service_date) != 2:
                raise RosterError("third-day invitation requires exactly two existing days")
            request = ThirdDayRequest(
                coordinator_id=coordinator_id,
                volunteer_id=volunteer_id,
                slot_id=slot_id,
                justification=justification.strip(),
            )
            db.add(request)
            db.flush()
            return request.id

    def respond_third_day(
        self, volunteer_id: int, request_id: int, accept: bool, *, db: Session | None = None
    ) -> int | None:
        with self._transaction(db) as db:
            original = db.get(ThirdDayRequest, request_id)
            if original is None or original.volunteer_id != volunteer_id:
                raise NotAuthorized("invitation not found for this volunteer")
            member, slot = self._lock_member_slot(db, volunteer_id, original.slot_id)
            request = db.scalar(
                select(ThirdDayRequest)
                .where(ThirdDayRequest.id == request_id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
            if request.status != "pending":
                raise RosterError("invitation already answered")
            request.responded_at = datetime.now(UTC)
            if not accept:
                request.status = "declined"
                return None
            service = db.get(WorshipService, slot.service_id)
            if self._monthly_days(db, volunteer_id, service.service_date) != 2:
                raise MonthlyLimit("monthly participation changed; request a new authorization")
            assignment_id = self._assign_locked(
                db, member, slot, allow_third=True, request_id=request.id
            )
            request.status = "accepted"
            return assignment_id

    def monthly_days(self, volunteer_id: int, day: date, *, db: Session | None = None) -> int:
        with self.session_factory() if db is None else nullcontext(db) as db:
            return self._monthly_days(db, volunteer_id, day)

    def capacity_report(self, ministry_id: int, day: date) -> CapacityReport:
        start, end = month_bounds(day)
        with self.session_factory() as db:
            services = db.scalars(
                select(WorshipService).where(
                    WorshipService.ministry_id == ministry_id,
                    WorshipService.service_date >= start,
                    WorshipService.service_date < end,
                )
            ).all()
            service_ids = [service.id for service in services]
            if not service_ids:
                return CapacityReport(0, 0, 0, ())
            slots = db.scalars(select(Slot).where(Slot.service_id.in_(service_ids))).all()
            needed = sum(slot.capacity for slot in slots)
            ministry = db.get(Ministry, ministry_id)
            active_members = (
                db.scalar(
                    select(func.count(Volunteer.id)).where(
                        Volunteer.church_id == ministry.church_id, Volunteer.active.is_(True)
                    )
                )
                or 0
            )
            normal = active_members * 2
            warnings = []
            conflict_pairs = {
                frozenset((row.role_a_id, row.role_b_id))
                for row in db.scalars(select(RoleConflict)).all()
            }
            lower_bound_by_date: dict[date, int] = {}
            for service in services:
                service_slots = [slot for slot in slots if slot.service_id == service.id]
                mandatory_people = 0
                # Every mutually incompatible set requires separate people at this service.
                for count in range(1, len(service_slots) + 1):
                    for group in combinations(service_slots, count):
                        if all(
                            frozenset((a.role_id, b.role_id)) in conflict_pairs
                            for a, b in combinations(group, 2)
                        ):
                            mandatory_people = max(
                                mandatory_people, sum(slot.capacity for slot in group)
                            )
                lower_bound_by_date[service.service_date] = max(
                    lower_bound_by_date.get(service.service_date, 0), mandatory_people
                )
            required_days_at_least = sum(lower_bound_by_date.values())
            if required_days_at_least > normal:
                warnings.append(
                    f"at least {required_days_at_least} volunteer-days are required; "
                    f"normal capacity is {normal}"
                )
            for slot in slots:
                eligible = (
                    db.scalar(
                        select(func.count(Volunteer.id))
                        .join(VolunteerRole, VolunteerRole.volunteer_id == Volunteer.id)
                        .where(
                            VolunteerRole.role_id == slot.role_id,
                            Volunteer.church_id == ministry.church_id,
                            Volunteer.active.is_(True),
                        )
                    )
                    or 0
                )
                if slot.capacity > eligible:
                    warnings.append(
                        f"slot {slot.id} needs {slot.capacity}, but only {eligible} "
                        "active volunteers qualify"
                    )
            return CapacityReport(
                needed, normal, max(0, required_days_at_least - normal), tuple(warnings)
            )

    def _lock_member_slot(self, db: Session, volunteer_id: int, slot_id: int):
        # The lock order is consistent across registration and exception acceptance.
        member = db.get(Volunteer, volunteer_id, with_for_update=True)
        if member is None or not member.active:
            raise NotFound("active volunteer not found")
        slot = db.get(Slot, slot_id, with_for_update=True)
        if slot is None:
            raise NotFound("slot not found")
        return member, slot

    def _assign_locked(
        self,
        db: Session,
        member: Volunteer,
        slot: Slot,
        *,
        allow_third: bool = False,
        request_id: int | None = None,
        allow_published: bool = False,
        reservation: Assignment | None = None,
    ) -> int:
        self._validate_assignment(
            db, member, slot, allow_third=allow_third, allow_published=allow_published
        )
        assignment = reservation or Assignment(
            slot_id=slot.id, volunteer_id=member.id, exception_id=request_id
        )
        assignment.status = "confirmed"
        db.add(assignment)
        db.flush()
        return assignment.id

    def _validate_assignment(
        self,
        db: Session,
        member: Volunteer,
        slot: Slot,
        *,
        allow_third: bool = False,
        allow_published: bool = False,
        projected: Sequence[AssignmentIntent] = (),
    ) -> None:
        service = db.get(WorshipService, slot.service_id)
        schedule = db.scalar(
            select(MonthlySchedule).where(
                MonthlySchedule.ministry_id == service.ministry_id,
                MonthlySchedule.year == service.service_date.year,
                MonthlySchedule.month == service.service_date.month,
            )
        )
        allowed_states = {"open", "published"} if allow_published or allow_third else {"open"}
        if schedule is not None and schedule.state not in allowed_states:
            raise RosterError("schedule does not accept this operation")
        projected_slots = (
            {
                s.id: (s, worship)
                for s, worship in db.execute(
                    select(Slot, WorshipService)
                    .join(WorshipService, WorshipService.id == Slot.service_id)
                    .where(Slot.id.in_({intent.slot_id for intent in projected}))
                )
            }
            if projected
            else {}
        )
        if len(projected_slots) != len({intent.slot_id for intent in projected}):
            raise NotFound("projected slot not found")
        projected_roles = [
            projected_slots[intent.slot_id][0].role_id
            for intent in projected
            if intent.volunteer_id == member.id
            and projected_slots[intent.slot_id][1].id == service.id
        ]
        self._check_eligibility(db, member, slot, service, projected_roles=projected_roles)
        occupied = (
            db.scalar(
                select(func.count(Assignment.id)).where(
                    Assignment.slot_id == slot.id, Assignment.status.in_(OCCUPYING)
                )
            )
            or 0
        )
        occupied += sum(intent.slot_id == slot.id for intent in projected)
        if occupied >= slot.capacity:
            raise NoCapacity("slot is full")
        days = self._monthly_days(db, member.id, service.service_date)
        already_on_day = bool(
            db.scalar(
                select(Assignment.id)
                .join(Slot, Slot.id == Assignment.slot_id)
                .join(WorshipService, WorshipService.id == Slot.service_id)
                .where(
                    Assignment.volunteer_id == member.id,
                    Assignment.status.in_(COUNTED_DAYS),
                    WorshipService.service_date == service.service_date,
                )
                .limit(1)
            )
        )
        if projected:
            start, end = month_bounds(service.service_date)
            existing_dates = set(
                db.scalars(
                    select(WorshipService.service_date)
                    .join(Slot, Slot.service_id == WorshipService.id)
                    .join(Assignment, Assignment.slot_id == Slot.id)
                    .where(
                        Assignment.volunteer_id == member.id,
                        Assignment.status.in_(COUNTED_DAYS),
                        WorshipService.service_date >= start,
                        WorshipService.service_date < end,
                    )
                )
            )
            existing_dates.update(
                projected_slots[intent.slot_id][1].service_date
                for intent in projected
                if intent.volunteer_id == member.id
                and start <= projected_slots[intent.slot_id][1].service_date < end
            )
            days = len(existing_dates)
            already_on_day = service.service_date in existing_dates
        if not already_on_day and days >= (3 if allow_third else 2):
            raise MonthlyLimit("monthly day limit reached")

    def _check_eligibility(
        self,
        db: Session,
        member: Volunteer,
        slot: Slot,
        service: WorshipService,
        *,
        projected_roles: Sequence[int] = (),
    ) -> None:
        ministry = db.get(Ministry, service.ministry_id)
        if ministry.church_id != member.church_id:
            raise RoleMismatch("volunteer belongs to another church")
        role = db.get(Role, slot.role_id)
        if role is None or not role.active:
            raise RoleMismatch("role is not active")
        if (
            db.scalar(
                select(VolunteerRole.id).where(
                    VolunteerRole.volunteer_id == member.id, VolunteerRole.role_id == slot.role_id
                )
            )
            is None
        ):
            raise RoleMismatch("volunteer cannot serve in this role")
        if (
            db.scalar(
                select(Unavailability.id).where(
                    Unavailability.volunteer_id == member.id,
                    Unavailability.start_date <= service.service_date,
                    Unavailability.end_date >= service.service_date,
                )
            )
            is not None
        ):
            raise Unavailable("volunteer is unavailable on this date")
        existing = db.execute(
            select(Assignment.id, Slot.role_id)
            .join(Slot, Slot.id == Assignment.slot_id)
            .where(
                Slot.service_id == service.id,
                Assignment.volunteer_id == member.id,
                Assignment.status.in_(COUNTED_DAYS),
            )
        ).all()
        role_ids = [role_id for _, role_id in existing] + list(projected_roles)
        if slot.role_id in role_ids:
            raise RosterError("volunteer is already assigned to this slot")
        if (
            role_ids
            and db.scalar(
                select(RoleConflict.id).where(
                    or_(
                        and_(
                            RoleConflict.role_a_id == slot.role_id,
                            RoleConflict.role_b_id.in_(role_ids),
                        ),
                        and_(
                            RoleConflict.role_b_id == slot.role_id,
                            RoleConflict.role_a_id.in_(role_ids),
                        ),
                    )
                )
            )
            is not None
        ):
            raise RoleConflictError("role conflicts with another assignment at this service")

    @staticmethod
    def _monthly_days(db: Session, volunteer_id: int, day: date) -> int:
        start, end = month_bounds(day)
        return (
            db.scalar(
                select(func.count(distinct(WorshipService.service_date)))
                .join(Slot, Slot.service_id == WorshipService.id)
                .join(Assignment, Assignment.slot_id == Slot.id)
                .where(
                    Assignment.volunteer_id == volunteer_id,
                    Assignment.status.in_(COUNTED_DAYS),
                    WorshipService.service_date >= start,
                    WorshipService.service_date < end,
                )
            )
            or 0
        )

    @staticmethod
    def _roles_belong_to_church(db: Session, church_id: int, role_ids: set[int]) -> bool:
        if not role_ids:
            return False
        found = set(
            db.scalars(
                select(Role.id)
                .join(Ministry, Ministry.id == Role.ministry_id)
                .where(Ministry.church_id == church_id, Role.id.in_(role_ids))
            ).all()
        )
        return found == role_ids
