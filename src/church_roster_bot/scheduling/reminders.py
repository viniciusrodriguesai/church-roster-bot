"""Opt-in reminders: church civil clocks, UTC snapshots and durable idempotency."""

import logging
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfoNotFoundError

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from church_roster_bot.administration.service import authorize_actor
from church_roster_bot.core.config import Settings
from church_roster_bot.core.time import local_to_utc, utc_now
from church_roster_bot.members.models import Volunteer
from church_roster_bot.ministries.models import Church, Ministry
from church_roster_bot.notifications.coordinators import notify_ministry
from church_roster_bot.notifications.reminders import notify_reminder_delivery, prepare_reminder
from church_roster_bot.scheduling.errors import RosterError
from church_roster_bot.scheduling.lifecycle import audit, schedule_for
from church_roster_bot.scheduling.models import Assignment, ReminderPolicy
from church_roster_bot.services.models import Slot, WorshipService
from church_roster_bot.services.timing import service_fingerprint, service_start
from church_roster_bot.whatsapp.models import OutboundMessage
from church_roster_bot.whatsapp.payloads import text_payload

logger = logging.getLogger(__name__)


def configure_reminders(
    db: Session,
    ministry_id: int,
    actor_type: str,
    actor_id: int,
    *,
    enabled: bool,
    days: int = 1,
    hour: time = time(9),
) -> None:
    if not 0 <= days <= 30 or hour.tzinfo is not None or hour.second or hour.microsecond:
        raise RosterError("use days 0..30 and a local HH:MM time")
    with db.begin_nested():
        ministry = authorize_actor(db, ministry_id, actor_type, actor_id)
        policy = db.get(ReminderPolicy, ministry_id, with_for_update=True)
        if policy is None:
            try:
                with db.begin_nested():
                    policy = ReminderPolicy(
                        ministry_id=ministry_id,
                        enabled=False,
                        days_before=days,
                        local_time=hour,
                        updated_at=utc_now(),
                    )
                    db.add(policy)
                    db.flush()
            except IntegrityError:
                policy = db.get(ReminderPolicy, ministry_id, with_for_update=True)
                if policy is None:
                    raise
        policy.enabled = enabled
        if enabled:
            policy.days_before, policy.local_time = days, hour
        policy.updated_at = utc_now()
        audit(
            db,
            ministry.church_id,
            actor_type,
            actor_id,
            "reminder_policy_changed",
            "ministry",
            ministry_id,
            {"enabled": enabled, "days": policy.days_before, "hour": policy.local_time.isoformat()},
        )


def enqueue_reminders(db: Session, config: Settings, *, now: datetime | None = None) -> int:
    instant = utc_now(now)
    assignments = db.scalars(
        select(Assignment)
        .join(Slot, Slot.id == Assignment.slot_id)
        .join(WorshipService, WorshipService.id == Slot.service_id)
        .join(ReminderPolicy, ReminderPolicy.ministry_id == WorshipService.ministry_id)
        .where(
            Assignment.status == "confirmed",
            ReminderPolicy.enabled.is_(True),
            WorshipService.service_date >= instant.date() - timedelta(days=1),
        )
        .order_by(Assignment.id)
    ).all()
    if not assignments:
        return 0
    for model, ids in (
        (Volunteer, {a.volunteer_id for a in assignments}),
        (Slot, {a.slot_id for a in assignments}),
        (Assignment, {a.id for a in assignments}),
    ):
        list(
            db.scalars(
                select(model)
                .where(model.id.in_(ids))
                .order_by(model.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        )
    created = 0
    for assignment in assignments:
        slot = db.get(Slot, assignment.slot_id)
        service = db.get(WorshipService, slot.service_id)
        policy = db.get(ReminderPolicy, service.ministry_id, populate_existing=True)
        ministry = db.get(Ministry, service.ministry_id)
        church = db.get(Church, ministry.church_id)
        schedule = schedule_for(db, ministry.id, service.service_date)
        if (
            assignment.status != "confirmed"
            or not policy.enabled
            or schedule is None
            or schedule.state in {"draft", "cancelled"}
        ):
            continue
        try:
            start = service_start(db, service)
            due = local_to_utc(
                service.service_date - timedelta(days=policy.days_before),
                policy.local_time,
                church.timezone,
            )
            if due >= start:
                raise ValueError("reminder must precede the service")
        except (ValueError, ZoneInfoNotFoundError):
            notify_ministry(
                db,
                ministry.id,
                f"reminder:{service.id}:invalid_time",
                f"Lembrete do culto #{service.id}: horário inválido ou posterior ao culto. "
                "Revise a configuração.",
                config,
            )
            continue
        if not due <= instant < start:
            continue
        outgoing = db.scalar(
            select(OutboundMessage).where(OutboundMessage.reminder_assignment_id == assignment.id)
        )
        if outgoing:
            if outgoing.status == "blocked_time" and outgoing.attempts == 0:
                outgoing.status = "pending"
                prepare_reminder(db, outgoing, config, now=instant)
            continue
        outgoing = OutboundMessage(
            reminder_assignment_id=assignment.id,
            volunteer_id=assignment.volunteer_id,
            reminder_due_at=due,
            reminder_fingerprint=service_fingerprint(db, slot),
            recipient_e164=db.get(Volunteer, assignment.volunteer_id).phone_e164,
            dedupe_key=f"reminder:{assignment.id}",
            payload=text_payload("Lembrete"),
            status="pending",
        )
        prepare_reminder(db, outgoing, config, now=instant)
        db.add(outgoing)
        if outgoing.status in {"blocked_consent", "blocked_window"}:
            notify_reminder_delivery(db, outgoing, config)
        audit(
            db,
            ministry.church_id,
            "system",
            0,
            "reminder_queued",
            "assignment",
            assignment.id,
            {"due_at": due.isoformat()},
        )
        created += 1
    if created:
        logger.info("reminders_queued", extra={"count": created})
    return created
