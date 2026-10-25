"""Common creation boundary for durable automation offers, never assignments."""

from datetime import datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from church_roster_bot.core.config import Settings
from church_roster_bot.core.time import utc_now
from church_roster_bot.notifications.automation import (
    enqueue_offer,
    service_fingerprint,
    service_start,
)
from church_roster_bot.scheduling.errors import RosterError
from church_roster_bot.scheduling.models import AutomationInvitation, EmergencyPolicy
from church_roster_bot.services.models import Slot, WorshipService


def create_invitation(
    db: Session,
    config: Settings,
    *,
    kind: str,
    source_key: str,
    volunteer_id: int,
    slot_id: int,
    reason: str,
    proposal_item_id: int | None = None,
    replacement_request_id: int | None = None,
    now: datetime | None = None,
) -> AutomationInvitation:
    """Caller owns the member/slot locks and authorizes the source operation."""
    existing = db.scalar(
        select(AutomationInvitation).where(AutomationInvitation.source_key == source_key)
    )
    if existing:
        return existing
    if not reason.strip() or len(reason) > 1000:
        raise RosterError("invitation reason must have 1..1000 characters")
    instant = utc_now(now)
    slot = db.get(Slot, slot_id)
    service = db.get(WorshipService, slot.service_id)
    start = service_start(db, service)
    if start <= instant:
        raise RosterError("service has already started")
    lifetime = timedelta(hours=config.automation_invite_ttl_hours)
    emergency = db.get(EmergencyPolicy, service.ministry_id)
    if (
        emergency
        and emergency.enabled
        and start - instant <= timedelta(hours=emergency.horizon_hours)
    ):
        lifetime = min(lifetime, timedelta(minutes=emergency.response_minutes))
        reason = (
            f"Urgente: responda em até {emergency.response_minutes} minuto(s); "
            f"validade limitada ao início do culto. {reason}"
        )[:1000]
    offer = AutomationInvitation(
        kind=kind,
        source_key=source_key,
        proposal_item_id=proposal_item_id,
        replacement_request_id=replacement_request_id,
        volunteer_id=volunteer_id,
        slot_id=slot_id,
        reason=reason,
        created_at=instant,
        expires_at=min(start, instant + lifetime),
        service_fingerprint=service_fingerprint(db, slot),
    )
    db.add(offer)
    db.flush()
    enqueue_offer(db, offer, config)
    return offer
