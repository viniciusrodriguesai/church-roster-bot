"""Validate durable reminder snapshots again immediately before transport."""

from datetime import datetime
from zoneinfo import ZoneInfoNotFoundError

from sqlalchemy.orm import Session

from church_roster_bot.core.config import Settings
from church_roster_bot.core.time import database_utc, utc_now
from church_roster_bot.ministries.models import Role
from church_roster_bot.notifications.coordinators import notify_ministry
from church_roster_bot.notifications.members import prepare_member_notice
from church_roster_bot.scheduling.lifecycle import schedule_for
from church_roster_bot.scheduling.models import Assignment, ReminderPolicy
from church_roster_bot.services.models import Slot, WorshipService
from church_roster_bot.services.timing import service_fingerprint, service_start
from church_roster_bot.whatsapp.models import OutboundMessage
from church_roster_bot.whatsapp.payloads import text_payload


def prepare_reminder(
    db: Session, outgoing: OutboundMessage, config: Settings, *, now: datetime | None = None
) -> None:
    instant = utc_now(now)
    assignment = db.get(Assignment, outgoing.reminder_assignment_id)
    slot = db.get(Slot, assignment.slot_id)
    service = db.get(WorshipService, slot.service_id)
    policy = db.get(ReminderPolicy, service.ministry_id)
    schedule = schedule_for(db, service.ministry_id, service.service_date)
    try:
        valid = (
            assignment.status == "confirmed"
            and assignment.volunteer_id == outgoing.volunteer_id
            and policy is not None
            and policy.enabled
            and schedule is not None
            and schedule.state not in {"draft", "cancelled"}
            and service_fingerprint(db, slot) == outgoing.reminder_fingerprint
            and instant < service_start(db, service)
        )
    except (ValueError, ZoneInfoNotFoundError):
        valid = False
    if not valid:
        outgoing.status, outgoing.error_code = "cancelled", "reminder_obsolete"
        return
    if instant < database_utc(outgoing.reminder_due_at):
        outgoing.status, outgoing.error_code = "blocked_time", "reminder_not_due"
        return
    role = db.get(Role, slot.role_id)
    outgoing.payload = text_payload(
        f"Lembrete de participação #{assignment.id}: {service.service_date:%d/%m/%Y} "
        f"às {service.start_time:%H:%M} — {role.name}. "
        "Se não puder participar, informe sua indisponibilidade para iniciar uma substituição."
    )
    prepare_member_notice(
        db,
        outgoing,
        config.model_copy(
            update={"whatsapp_notification_template": config.whatsapp_reminder_template}
        ),
    )


def notify_reminder_delivery(db: Session, outgoing: OutboundMessage, config: Settings) -> None:
    assignment = db.get(Assignment, outgoing.reminder_assignment_id)
    service = db.get(WorshipService, db.get(Slot, assignment.slot_id).service_id)
    notify_ministry(
        db,
        service.ministry_id,
        f"reminder:{assignment.id}:{outgoing.status}",
        f"Lembrete da participação #{assignment.id}: {outgoing.status}. "
        f"Código: {outgoing.error_code or 'sem código'}.",
        config,
    )
