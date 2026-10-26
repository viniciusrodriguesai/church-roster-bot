"""Scoped coordinator alerts with authority and recipient revalidation at claim."""

import logging

from sqlalchemy import select
from sqlalchemy.orm import Session

from church_roster_bot.core.config import Settings
from church_roster_bot.members.models import Coordinator, CoordinatorMinistry
from church_roster_bot.ministries.models import Ministry
from church_roster_bot.notifications.outbox import apply_window
from church_roster_bot.whatsapp.models import OutboundMessage
from church_roster_bot.whatsapp.payloads import text_payload

logger = logging.getLogger(__name__)


def prepare_coordinator_notice(db: Session, outgoing: OutboundMessage, config: Settings) -> None:
    permitted = None
    if outgoing.coordinator_id is not None and outgoing.notice_ministry_id is not None:
        # Select columns rather than cached ORM entities: current grants and phone
        # determine whether this specific pending message may leave the outbox.
        permitted = db.scalar(
            select(Coordinator.id)
            .join(CoordinatorMinistry, CoordinatorMinistry.coordinator_id == Coordinator.id)
            .join(Ministry, Ministry.id == CoordinatorMinistry.ministry_id)
            .where(
                Coordinator.id == outgoing.coordinator_id,
                Coordinator.active.is_(True),
                Coordinator.church_id == Ministry.church_id,
                Coordinator.phone_e164 == outgoing.recipient_e164,
                Ministry.id == outgoing.notice_ministry_id,
            )
        )
    if permitted is None:
        outgoing.status, outgoing.error_code = "cancelled", "coordinator_authority_changed"
        logger.info("coordinator_notice_cancelled", extra={"outgoing_id": outgoing.id})
        return
    outgoing.error_code = None
    apply_window(db, outgoing, config)


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
            recipient_e164=coordinator.phone_e164,
            coordinator_id=coordinator.id,
            notice_ministry_id=ministry_id,
            payload=text_payload(body),
            dedupe_key=dedupe,
        )
        prepare_coordinator_notice(db, outgoing, config)
        db.add(outgoing)
        count += 1
    return count
