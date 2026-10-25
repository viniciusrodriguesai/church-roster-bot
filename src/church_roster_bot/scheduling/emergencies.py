"""Urgent capacity offers with explicit acceptance, FIFO precedence and escalation."""

import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfoNotFoundError

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from church_roster_bot.administration.service import authorize_actor
from church_roster_bot.core.config import Settings
from church_roster_bot.core.time import utc_now
from church_roster_bot.members.models import Volunteer
from church_roster_bot.ministries.models import Ministry
from church_roster_bot.notifications.automation import notify_offer
from church_roster_bot.notifications.coordinators import notify_ministry
from church_roster_bot.scheduling.candidates import ranked_candidates
from church_roster_bot.scheduling.engine import OCCUPYING, RosterEngine
from church_roster_bot.scheduling.errors import RosterError
from church_roster_bot.scheduling.invitations import create_invitation
from church_roster_bot.scheduling.lifecycle import audit, schedule_for
from church_roster_bot.scheduling.models import (
    Assignment,
    AutomationInvitation,
    EmergencyPolicy,
    WaitlistEntry,
)
from church_roster_bot.services.models import Slot, WorshipService
from church_roster_bot.services.timing import service_fingerprint, service_start

logger = logging.getLogger(__name__)


def configure_emergencies(
    db: Session,
    ministry_id: int,
    actor_type: str,
    actor_id: int,
    *,
    enabled: bool,
    hours: int = 6,
    minutes: int = 15,
) -> None:
    if not 1 <= hours <= 72 or not 1 <= minutes <= 120:
        raise RosterError("use emergency horizon 1..72 hours and response 1..120 minutes")
    with db.begin_nested():
        ministry = authorize_actor(db, ministry_id, actor_type, actor_id)
        policy = db.get(EmergencyPolicy, ministry_id, with_for_update=True)
        if policy is None:
            try:
                with db.begin_nested():
                    policy = EmergencyPolicy(
                        ministry_id=ministry_id,
                        enabled=False,
                        horizon_hours=hours,
                        response_minutes=minutes,
                        updated_at=utc_now(),
                    )
                    db.add(policy)
                    db.flush()
            except IntegrityError:
                policy = db.get(EmergencyPolicy, ministry_id, with_for_update=True)
                if policy is None:
                    raise
        policy.enabled = enabled
        if enabled:
            policy.horizon_hours, policy.response_minutes = hours, minutes
        policy.updated_at = utc_now()
        audit(
            db,
            ministry.church_id,
            actor_type,
            actor_id,
            "emergency_policy_changed",
            "ministry",
            ministry_id,
            {"enabled": enabled, "hours": policy.horizon_hours, "minutes": policy.response_minutes},
        )


def _queue_has_priority(db: Session, roster: RosterEngine, slot_id: int) -> bool:
    fingerprint = service_fingerprint(db, db.get(Slot, slot_id))
    for entry, member in db.execute(
        select(WaitlistEntry, Volunteer)
        .select_from(WaitlistEntry)
        .join(Volunteer, Volunteer.id == WaitlistEntry.volunteer_id)
        .where(
            WaitlistEntry.slot_id == slot_id,
            WaitlistEntry.status == "waiting",
            Volunteer.active.is_(True),
            Volunteer.invite_opt_in.is_(True),
        )
    ):
        try:
            if entry.service_fingerprint != fingerprint:
                continue
            roster.check_assignment(db, member.id, slot_id, allow_published=True)
        except RosterError:
            continue
        return True
    return False


def dispatch_emergencies(
    db: Session, roster: RosterEngine, config: Settings, *, now: datetime | None = None
) -> int:
    instant = utc_now(now)
    potential = db.scalars(
        select(Slot)
        .join(WorshipService, WorshipService.id == Slot.service_id)
        .join(EmergencyPolicy, EmergencyPolicy.ministry_id == WorshipService.ministry_id)
        .where(
            EmergencyPolicy.enabled.is_(True),
            WorshipService.service_date >= instant.date() - timedelta(days=1),
            WorshipService.service_date <= instant.date() + timedelta(days=4),
        )
        .order_by(Slot.id)
    ).all()
    if not potential:
        return 0
    church_ids = set(
        db.scalars(
            select(Ministry.church_id)
            .join(WorshipService, WorshipService.ministry_id == Ministry.id)
            .join(Slot, Slot.service_id == WorshipService.id)
            .where(Slot.id.in_([s.id for s in potential]))
        )
    )
    list(
        db.scalars(
            select(Volunteer)
            .where(Volunteer.church_id.in_(church_ids))
            .order_by(Volunteer.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    )
    slots = db.scalars(
        select(Slot)
        .where(Slot.id.in_([s.id for s in potential]))
        .order_by(Slot.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).all()
    urgent = []
    for slot in slots:
        service = db.get(WorshipService, slot.service_id)
        policy = db.get(EmergencyPolicy, service.ministry_id, populate_existing=True)
        try:
            start = service_start(db, service)
        except (ValueError, ZoneInfoNotFoundError):
            notify_ministry(
                db,
                service.ministry_id,
                f"emergency:{slot.id}:invalid_time",
                f"Emergência da vaga #{slot.id}: horário ou fuso inválido; revise o culto.",
                config,
            )
            continue
        if policy.enabled and instant < start <= instant + timedelta(hours=policy.horizon_hours):
            urgent.append((start, slot))
    created = 0
    for _, slot in sorted(urgent, key=lambda item: (item[0], item[1].id)):
        service = db.get(WorshipService, slot.service_id)
        ministry = db.get(Ministry, service.ministry_id)
        schedule = schedule_for(db, ministry.id, service.service_date)
        if schedule is None or schedule.state not in {"open", "published"}:
            continue
        for current in db.scalars(
            select(AutomationInvitation)
            .where(
                AutomationInvitation.slot_id == slot.id,
                AutomationInvitation.kind == "emergency",
                AutomationInvitation.status == "pending",
            )
            .order_by(AutomationInvitation.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        ).all():
            member = db.get(Volunteer, current.volunteer_id)
            try:
                if not member.invite_opt_in or current.service_fingerprint != service_fingerprint(
                    db, slot
                ):
                    raise RosterError("consent or service changed")
                roster.check_assignment(db, member.id, slot.id, allow_published=True)
            except RosterError:
                current.status, current.responded_at = "cancelled", instant
                notify_offer(db, current, "cancelled", config)
        occupied = (
            db.scalar(
                select(func.count(Assignment.id)).where(
                    Assignment.slot_id == slot.id, Assignment.status.in_(OCCUPYING)
                )
            )
            or 0
        )
        pending = (
            db.scalar(
                select(func.count(AutomationInvitation.id)).where(
                    AutomationInvitation.slot_id == slot.id,
                    AutomationInvitation.kind != "replacement",
                    AutomationInvitation.status == "pending",
                )
            )
            or 0
        )
        positions = max(0, slot.capacity - occupied - pending)
        if pending and occupied < slot.capacity:
            notify_ministry(
                db,
                ministry.id,
                f"emergency:{slot.id}:awaiting",
                f"Vaga urgente #{slot.id}: cobertura ainda depende de aceite de convite pendente. "
                "Nenhuma participação foi confirmada automaticamente.",
                config,
            )
        if positions == 0 or _queue_has_priority(db, roster, slot.id):
            continue
        tried = set(
            db.scalars(
                select(AutomationInvitation.volunteer_id).where(
                    AutomationInvitation.slot_id == slot.id
                )
            )
        )
        candidates = ranked_candidates(db, roster, slot.id, excluded=tried, now=instant)
        for member_id, score in candidates[:positions]:
            offer = create_invitation(
                db,
                config,
                kind="emergency",
                source_key=f"emergency:{slot.id}:{member_id}",
                volunteer_id=member_id,
                slot_id=slot.id,
                reason=f"Cobertura urgente de vaga aberta. {score.explanation}",
                now=instant,
            )
            audit(
                db,
                ministry.church_id,
                "system",
                0,
                "emergency_offered",
                "automation_invitation",
                offer.id,
                {"slot_id": slot.id},
            )
            created += 1
        if len(candidates) < positions:
            notify_ministry(
                db,
                ministry.id,
                f"emergency:{slot.id}:no_candidate",
                f"Emergência na vaga #{slot.id}: {positions - len(candidates)} posição(ões) "
                "sem candidato elegível disponível. Revise a cobertura; "
                "nenhuma regra foi ultrapassada.",
                config,
            )
    if created:
        logger.info("emergency_offers_created", extra={"count": created})
    return created
