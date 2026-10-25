"""Idempotent notices whose recipient and consent are rechecked immediately before sending."""

from sqlalchemy import select
from sqlalchemy.orm import Session

from church_roster_bot.core.config import Settings
from church_roster_bot.members.models import Volunteer
from church_roster_bot.notifications.outbox import apply_window
from church_roster_bot.whatsapp.models import OutboundMessage
from church_roster_bot.whatsapp.payloads import text_payload


def prepare_member_notice(db: Session, outgoing: OutboundMessage, config: Settings) -> None:
    member = db.get(Volunteer, outgoing.volunteer_id)
    outgoing.error_code = None
    if (
        not member
        or not member.active
        or not member.invite_opt_in
        or member.phone_e164 != outgoing.recipient_e164
    ):
        outgoing.status, outgoing.error_code = "blocked_consent", "consent_or_recipient_changed"
        return
    apply_window(db, outgoing, config)


def member_notice(db: Session, volunteer_id: int, key: str, body: str, config: Settings) -> None:
    dedupe = f"member:{volunteer_id}:{key}"
    if db.scalar(select(OutboundMessage.id).where(OutboundMessage.dedupe_key == dedupe)):
        return
    member = db.get(Volunteer, volunteer_id)
    outgoing = OutboundMessage(
        volunteer_id=volunteer_id,
        recipient_e164=member.phone_e164,
        dedupe_key=dedupe,
        payload=text_payload(body),
        status="pending",
    )
    prepare_member_notice(db, outgoing, config)
    db.add(outgoing)


def activate_member_notices(db: Session, volunteer_id: int, config: Settings) -> None:
    for outgoing in db.scalars(
        select(OutboundMessage).where(
            OutboundMessage.volunteer_id == volunteer_id,
            OutboundMessage.attempts == 0,
            OutboundMessage.status.in_(("blocked_consent", "blocked_window")),
        )
    ).all():
        outgoing.status = "pending"
        prepare_member_notice(db, outgoing, config)
