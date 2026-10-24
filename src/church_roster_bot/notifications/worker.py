"""Run one durable inbox event and one outbound attempt at a time."""

import logging
import time
from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import select

from church_roster_bot.conversations.models import ConversationSession
from church_roster_bot.conversations.router import Bot, aware
from church_roster_bot.core.config import Settings, settings
from church_roster_bot.core.database import SessionLocal
from church_roster_bot.whatsapp.client import MetaSendError, WhatsAppClient
from church_roster_bot.whatsapp.models import InboundEvent, OutboundMessage
from church_roster_bot.whatsapp.payloads import parse_message

logger = logging.getLogger(__name__)


class Worker:
    def __init__(self, session_factory=SessionLocal, config: Settings = settings, client=None):
        self.session_factory = session_factory
        self.config = config
        self.client = client or WhatsAppClient(config)
        self.bot = Bot(session_factory, config)
        self._current_event_id: int | None = None

    def process_one(self) -> bool:
        self._current_event_id = None
        try:
            return self._process_one_transaction()
        except Exception as exc:
            # A failed business transaction rolls back before this receipt update.
            logger.error(
                "inbound_processing_failed",
                extra={"event_id": self._current_event_id, "error_type": type(exc).__name__},
            )
            if self._current_event_id is not None:
                with self.session_factory.begin() as db:
                    event = db.get(InboundEvent, self._current_event_id, with_for_update=True)
                    if event and event.status == "pending":
                        event.attempts += 1
                        event.error_code = type(exc).__name__[:64]
                        if event.attempts >= 3:
                            event.status = "failed"
                return True
            raise

    def _process_one_transaction(self) -> bool:
        # Business writes, session updates, outbound drafts and the event receipt commit together.
        with self.session_factory.begin() as db:
            event = db.scalar(
                select(InboundEvent)
                .where(InboundEvent.status == "pending")
                .order_by(InboundEvent.id)
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            if event is None:
                return False
            self._current_event_id = event.id
            if event.kind == "message":
                message = parse_message(event.payload)
                if message:
                    self.bot.handle(db, message)
                event.status = "processed" if message else "ignored"
            elif event.kind == "status":
                outgoing = db.scalar(
                    select(OutboundMessage).where(
                        OutboundMessage.meta_message_id == event.payload.get("id")
                    )
                )
                if outgoing:
                    status = event.payload.get("status")
                    if status in {"sent", "delivered", "read", "failed"}:
                        rank = {"sent": 1, "delivered": 2, "read": 3}
                        if status == "failed" or rank.get(status, 0) >= rank.get(
                            outgoing.status, 0
                        ):
                            outgoing.status = status
                        if status == "failed":
                            errors = event.payload.get("errors")
                            first_error = (
                                errors[0]
                                if isinstance(errors, list)
                                and errors
                                and isinstance(errors[0], dict)
                                else {}
                            )
                            outgoing.error_code = str(first_error.get("code", "unknown"))[:64]
                event.status = "processed"
            else:
                event.status = "ignored"
            event.attempts += 1
        return True

    def send_one(self) -> bool:
        with self.session_factory.begin() as db:
            outgoing = db.scalar(
                select(OutboundMessage)
                .where(OutboundMessage.status == "pending")
                .order_by(OutboundMessage.id)
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            if outgoing is None:
                return False
            if outgoing.payload.get("type") != "template":
                session = db.scalar(
                    select(ConversationSession).where(
                        ConversationSession.phone_e164 == outgoing.recipient_e164
                    )
                )
                if session is None or datetime.now(UTC) - aware(
                    session.last_inbound_at
                ) >= timedelta(hours=24):
                    outgoing.status = "blocked_window"
                    outgoing.error_code = "template_required"
                    return True
            try:
                outgoing.meta_message_id = self.client.send(
                    outgoing.recipient_e164, outgoing.payload
                )
                outgoing.status = "sent"
                outgoing.attempts += 1
            except MetaSendError as exc:
                outgoing.attempts += 1
                outgoing.error_code = exc.code
                if exc.status_code == 429 and outgoing.attempts < 3:
                    outgoing.status = "pending"
                else:
                    outgoing.status = "failed"
            except (httpx.TimeoutException, httpx.TransportError):
                # Acceptance by Meta is ambiguous after a network loss. Avoid a duplicate send.
                outgoing.attempts += 1
                outgoing.status = "unknown"
                outgoing.error_code = "network_ambiguous"
            except RuntimeError:
                outgoing.attempts += 1
                outgoing.status = "failed"
                outgoing.error_code = "credentials_missing"
        return True

    def run_forever(self, interval: float = 1.0) -> None:
        while True:
            try:
                progressed = self.process_one()
                progressed = self.send_one() or progressed
                if not progressed:
                    time.sleep(interval)
            except Exception as exc:
                logger.error("worker_cycle_failed", extra={"error_type": type(exc).__name__})
                time.sleep(5)


if __name__ == "__main__":
    Worker().run_forever()
