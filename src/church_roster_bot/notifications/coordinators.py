"""Scoped, deduplicated coordinator alerts shared by automation tasks."""

from sqlalchemy import select
from sqlalchemy.orm import Session

from church_roster_bot.core.config import Settings
from church_roster_bot.members.models import Coordinator, CoordinatorMinistry
from church_roster_bot.ministries.models import Ministry
from church_roster_bot.notifications.outbox import apply_window
from church_roster_bot.whatsapp.models import OutboundMessage
from church_roster_bot.whatsapp.payloads import text_payload


def notify_ministry(db: Session, ministry_id: int, key: str, body: str, config: Settings) -> int:
    ministry = db.get(Ministry, ministry_id)
    count = 0
    for coordinator in db.scalars(
        select(Coordinator)
        .join(CoordinatorMinistry, CoordinatorMinistry.coordinator_id == Coordinator.id)
        .where(
            CoordinatorMinistry.ministry_id == ministry_id,
            Coordinator.church_id == ministry.church_id,
            Coordinator.active.is_(True),
        )
        .order_by(Coordinator.id)
    ):
        dedupe = f"{key}:{coordinator.id}"
        if db.scalar(select(OutboundMessage.id).where(OutboundMessage.dedupe_key == dedupe)):
            continue
        outgoing = OutboundMessage(
            recipient_e164=coordinator.phone_e164, payload=text_payload(body), dedupe_key=dedupe
        )
        apply_window(db, outgoing, config)
        db.add(outgoing)
        count += 1
    return count
