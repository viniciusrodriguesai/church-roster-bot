"""Shared window policy for durable outbound messages."""

from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from church_roster_bot.conversations.models import ConversationSession
from church_roster_bot.core.config import Settings
from church_roster_bot.whatsapp.models import OutboundMessage
from church_roster_bot.whatsapp.payloads import template_payload


def window_open(db: Session, phone: str) -> bool:
    session = db.scalar(select(ConversationSession).where(ConversationSession.phone_e164 == phone))
    if session is None:
        return False
    last = session.last_inbound_at
    if last.tzinfo is None:
        last = last.replace(tzinfo=UTC)
    return timedelta(0) <= datetime.now(UTC) - last < timedelta(hours=24)


def apply_window(db: Session, outgoing: OutboundMessage, config: Settings) -> None:
    if outgoing.payload.get("type") == "template" or window_open(db, outgoing.recipient_e164):
        return
    if config.whatsapp_notification_template and outgoing.payload.get("type") == "text":
        outgoing.payload = template_payload(
            config.whatsapp_notification_template,
            config.whatsapp_template_language,
            [outgoing.payload["text"]["body"]],
        )
    else:
        outgoing.status = "blocked_window"
        outgoing.error_code = "template_required"
