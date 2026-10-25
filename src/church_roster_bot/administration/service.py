"""Authorized writes for conversation flows."""

from datetime import date, time
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from church_roster_bot.members.models import (
    Coordinator,
    CoordinatorMinistry,
    GeneralAdmin,
    Volunteer,
    VolunteerRole,
)
from church_roster_bot.ministries.models import Church, Ministry, Role
from church_roster_bot.scheduling.engine import OCCUPYING
from church_roster_bot.scheduling.errors import NotAuthorized, NotFound, RosterError
from church_roster_bot.scheduling.lifecycle import audit, schedule_for
from church_roster_bot.scheduling.models import Assignment
from church_roster_bot.services.models import Slot, WorshipService
from church_roster_bot.whatsapp.payloads import normalize_phone


def actor_for(db: Session, phone: str):
    admin = db.scalar(
        select(GeneralAdmin).where(GeneralAdmin.phone_e164 == phone, GeneralAdmin.active.is_(True))
    )
    coordinator = db.scalar(
        select(Coordinator).where(Coordinator.phone_e164 == phone, Coordinator.active.is_(True))
    )
    volunteer = db.scalar(
        select(Volunteer).where(Volunteer.phone_e164 == phone, Volunteer.active.is_(True))
    )
    return admin, coordinator, volunteer


def permitted_ministries(db: Session, admin: GeneralAdmin | None, coordinator: Coordinator | None):
    if admin:
        return db.scalars(select(Ministry).where(Ministry.church_id == admin.church_id)).all()
    if coordinator:
        return db.scalars(
            select(Ministry)
            .join(CoordinatorMinistry, CoordinatorMinistry.ministry_id == Ministry.id)
            .where(
                CoordinatorMinistry.coordinator_id == coordinator.id,
                Ministry.church_id == coordinator.church_id,
            )
        ).all()
    return []


def require_ministry(
    db: Session, ministry_id: int, admin: GeneralAdmin | None, coordinator: Coordinator | None
) -> Ministry:
    for ministry in permitted_ministries(db, admin, coordinator):
        if ministry.id == ministry_id:
            return ministry
    raise NotAuthorized("ministry is not authorized for this coordinator")


def edit_volunteer(
    db: Session,
    volunteer_id: int,
    field: str,
    value: str,
    ministry_id: int,
    actor_type: str,
    actor_id: int,
) -> None:
    ministry = db.get(Ministry, ministry_id)
    member = db.get(Volunteer, volunteer_id, with_for_update=True)
    if member is None or member.church_id != ministry.church_id:
        raise NotFound("volunteer not found in this church")
    if (
        db.scalar(
            select(VolunteerRole.id)
            .join(Role, Role.id == VolunteerRole.role_id)
            .where(VolunteerRole.volunteer_id == volunteer_id, Role.ministry_id == ministry_id)
        )
        is None
    ):
        raise NotAuthorized("volunteer is not in this ministry")
    if field == "name":
        if not value.strip() or len(value) > 160:
            raise RosterError("invalid name")
        member.name = value.strip()
    elif field == "phone":
        member.phone_e164 = normalize_phone(value)
    elif field == "active":
        member.active = value == "true"
    elif field == "roles":
        role_ids = {int(v.strip()) for v in value.split(",")}
        available = set(
            db.scalars(
                select(Role.id).where(Role.ministry_id == ministry_id, Role.active.is_(True))
            ).all()
        )
        if not role_ids or not role_ids <= available:
            raise RosterError("roles must belong to the authorized ministry")
        current = db.scalars(
            select(VolunteerRole)
            .join(Role, Role.id == VolunteerRole.role_id)
            .where(VolunteerRole.volunteer_id == volunteer_id, Role.ministry_id == ministry_id)
        ).all()
        removed = {row.role_id for row in current} - role_ids
        if removed:
            future = db.scalar(
                select(func.count(Assignment.id))
                .join(Slot, Slot.id == Assignment.slot_id)
                .join(WorshipService, WorshipService.id == Slot.service_id)
                .where(
                    Assignment.volunteer_id == volunteer_id,
                    Slot.role_id.in_(removed),
                    WorshipService.service_date >= date.today(),
                    Assignment.status.in_(OCCUPYING),
                )
            )
            if future:
                raise RosterError("cannot remove roles with future assignments")
        for row in current:
            db.delete(row)
        db.flush()
        db.add_all(VolunteerRole(volunteer_id=volunteer_id, role_id=rid) for rid in role_ids)
    else:
        raise RosterError("unknown edit field")
    audit(
        db,
        ministry.church_id,
        actor_type,
        actor_id,
        "edit_volunteer",
        "volunteer",
        member.id,
        {"field": field},
    )


def add_coordinator(
    db: Session, admin: GeneralAdmin, phone: str, name: str, ministry_ids: set[int]
) -> int:
    if not name.strip() or not ministry_ids:
        raise RosterError("name and ministries are required")
    allowed = set(
        db.scalars(select(Ministry.id).where(Ministry.church_id == admin.church_id)).all()
    )
    if not ministry_ids <= allowed:
        raise NotAuthorized("ministries must belong to the admin's church")
    coordinator = Coordinator(
        church_id=admin.church_id, name=name.strip(), phone_e164=normalize_phone(phone)
    )
    db.add(coordinator)
    db.flush()
    db.add_all(
        CoordinatorMinistry(coordinator_id=coordinator.id, ministry_id=mid) for mid in ministry_ids
    )
    audit(db, admin.church_id, "admin", admin.id, "add_coordinator", "coordinator", coordinator.id)
    return coordinator.id


def add_ministry(db: Session, admin: GeneralAdmin, name: str) -> int:
    if not name.strip() or len(name) > 100:
        raise RosterError("invalid ministry name")
    ministry = Ministry(church_id=admin.church_id, name=name.strip())
    db.add(ministry)
    db.flush()
    audit(db, admin.church_id, "admin", admin.id, "add_ministry", "ministry", ministry.id)
    return ministry.id


def edit_coordinator(
    db: Session, admin: GeneralAdmin, coordinator_id: int, field: str, value: str
) -> None:
    coordinator = db.get(Coordinator, coordinator_id, with_for_update=True)
    if coordinator is None or coordinator.church_id != admin.church_id:
        raise NotAuthorized("coordinator is outside this church")
    if field == "name":
        if not value.strip() or len(value) > 160:
            raise RosterError("invalid coordinator name")
        coordinator.name = value.strip()
    elif field == "phone":
        coordinator.phone_e164 = normalize_phone(value)
    elif field == "active":
        coordinator.active = value == "true"
    elif field == "ministries":
        ids = {int(v.strip()) for v in value.split(",")}
        allowed = set(db.scalars(select(Ministry.id).where(Ministry.church_id == admin.church_id)))
        if not ids or not ids <= allowed:
            raise NotAuthorized("ministries must belong to this church")
        current = db.scalars(
            select(CoordinatorMinistry).where(CoordinatorMinistry.coordinator_id == coordinator_id)
        ).all()
        for row in current:
            db.delete(row)
        db.flush()
        db.add_all(
            CoordinatorMinistry(coordinator_id=coordinator_id, ministry_id=mid) for mid in ids
        )
    else:
        raise RosterError("unknown coordinator field")
    audit(
        db,
        admin.church_id,
        "admin",
        admin.id,
        "edit_coordinator",
        "coordinator",
        coordinator_id,
        {"field": field},
    )


def add_role(db: Session, admin: GeneralAdmin, ministry_id: int, name: str) -> int:
    ministry = db.get(Ministry, ministry_id)
    if ministry is None or ministry.church_id != admin.church_id:
        raise NotAuthorized("ministry is outside this church")
    if not name.strip() or len(name) > 100:
        raise RosterError("invalid role name")
    role = Role(ministry_id=ministry_id, name=name.strip())
    db.add(role)
    db.flush()
    audit(db, admin.church_id, "admin", admin.id, "add_role", "role", role.id)
    return role.id


def set_church_timezone(db: Session, admin: GeneralAdmin, timezone: str) -> None:
    try:
        ZoneInfo(timezone)
    except ZoneInfoNotFoundError as exc:
        raise RosterError("unknown IANA timezone") from exc
    church = db.get(Church, admin.church_id, with_for_update=True)
    church.timezone = timezone
    audit(db, admin.church_id, "admin", admin.id, "set_timezone", "church", church.id)


def rename_ministry(db: Session, admin: GeneralAdmin, ministry_id: int, name: str) -> None:
    ministry = db.get(Ministry, ministry_id, with_for_update=True)
    if ministry is None or ministry.church_id != admin.church_id:
        raise NotAuthorized("ministry is outside this church")
    if not name.strip() or len(name) > 100:
        raise RosterError("invalid ministry name")
    ministry.name = name.strip()
    audit(db, admin.church_id, "admin", admin.id, "rename_ministry", "ministry", ministry.id)


def edit_service(
    db: Session,
    ministry_id: int,
    service_id: int,
    field: str,
    value: str,
    actor_type: str,
    actor_id: int,
) -> None:
    service = db.get(WorshipService, service_id, with_for_update=True)
    if service is None or service.ministry_id != ministry_id:
        raise NotAuthorized("service is outside the authorized ministry")
    schedule = schedule_for(db, ministry_id, service.service_date)
    if schedule and schedule.state in {"published", "closed", "cancelled"}:
        raise RosterError("published or closed service cannot be moved")
    occupied = (
        db.scalar(
            select(func.count(Assignment.id))
            .join(Slot, Slot.id == Assignment.slot_id)
            .where(Slot.service_id == service_id, Assignment.status.in_(OCCUPYING))
        )
        or 0
    )
    if occupied:
        raise RosterError("cannot move a service with active assignments")
    if field == "date":
        new_day = date.fromisoformat(value)
        target = schedule_for(db, ministry_id, new_day, create=True)
        if target.state in {"published", "closed", "cancelled"}:
            raise RosterError("target month does not accept service changes")
        service.service_date = new_day
    elif field == "time":
        service.start_time = time.fromisoformat(value)
    else:
        raise RosterError("unknown service field")
    ministry = db.get(Ministry, ministry_id)
    audit(
        db,
        ministry.church_id,
        actor_type,
        actor_id,
        "edit_service",
        "service",
        service_id,
        {"field": field},
    )
