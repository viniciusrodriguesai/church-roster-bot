"""Run one durable inbox event and one outbound attempt at a time."""

import logging
import time

import httpx
from sqlalchemy import select

from church_roster_bot.conversations.router import Bot
from church_roster_bot.core.config import Settings, settings
from church_roster_bot.core.database import SessionLocal, is_sqlite_busy, retry_sqlite_transaction
from church_roster_bot.notifications.automation import notify_offer, prepare_offer
from church_roster_bot.notifications.invitations import notify_coordinator, prepare_invitation
from church_roster_bot.notifications.outbox import apply_window
from church_roster_bot.scheduling.automation import open_monthly_schedules
from church_roster_bot.scheduling.models import AutomationInvitation, ThirdDayRequest
from church_roster_bot.scheduling.offers import expire_offers
from church_roster_bot.whatsapp.client import MetaSendError, WhatsAppClient
from church_roster_bot.whatsapp.models import InboundEvent, OutboundMessage
from church_roster_bot.whatsapp.payloads import parse_message

logger = logging.getLogger(__name__)


def lock_simulation_transaction(db) -> None:
    # SQLite has no row locks. Serialize claims across local simulator terminals.
    if db.bind.dialect.name == "sqlite":
        db.connection().exec_driver_sql("BEGIN IMMEDIATE")


class Worker:
    def __init__(self, session_factory=SessionLocal, config: Settings = settings, client=None):
        self.session_factory = session_factory
        self.config = config
        self.client = client or WhatsAppClient(config)
        self.bot = Bot(session_factory, config)
        self._current_event_id: int | None = None
        self._maintenance_after = 0.0

    def process_one(self) -> bool:
        self._current_event_id = None
        try:
            return self._process_one_transaction()
        except Exception as exc:
            if is_sqlite_busy(exc):
                return False
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
            lock_simulation_transaction(db)
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
                    select(OutboundMessage)
                    .where(OutboundMessage.meta_message_id == event.payload.get("id"))
                    .with_for_update()
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
                            self._notify_delivery(db, outgoing)
                event.status = "processed"
            else:
                event.status = "ignored"
            event.attempts += 1
        return True

    def _claim_outgoing(self) -> tuple[bool, tuple[int, str, dict] | None]:
        # Persist the claim BEFORE calling Meta. A crash must never cause an automatic resend.
        with self.session_factory.begin() as db:
            lock_simulation_transaction(db)
            outgoing = db.scalar(
                select(OutboundMessage)
                .where(OutboundMessage.status == "pending")
                .order_by(OutboundMessage.id)
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            if outgoing is None:
                return False, None
            if outgoing.automation_invitation_id:
                prepare_offer(db, outgoing, self.config)
            elif outgoing.invitation_id:
                prepare_invitation(db, outgoing, self.config)
            else:
                apply_window(db, outgoing, self.config)
            if outgoing.status != "pending":
                self._notify_delivery(db, outgoing)
                return True, None
            outgoing.status = "sending"
            outgoing.attempts += 1
            return True, (outgoing.id, outgoing.recipient_e164, outgoing.payload)

    def send_one(self) -> bool:
        try:
            progressed, claim = self._claim_outgoing()
        except Exception as exc:
            if is_sqlite_busy(exc):
                return False
            raise
        if claim is None:
            return progressed
        outgoing_id, recipient, payload = claim

        meta_id, status, error = None, "sent", None
        try:
            meta_id = self.client.send(recipient, payload)
        except MetaSendError as exc:
            error = exc.code[:64]
            # A malformed success response is also ambiguous: Meta may have accepted it.
            status = "unknown" if exc.status_code < 400 else "failed"
            if exc.status_code == 429:
                status = "pending"
        except (httpx.TimeoutException, httpx.TransportError):
            status, error = "unknown", "network_ambiguous"
        except RuntimeError:
            status, error = "failed", "credentials_missing"
        except Exception as exc:
            status, error = "unknown", type(exc).__name__[:64]

        def record_result() -> None:
            with self.session_factory.begin() as db:
                outgoing = db.get(OutboundMessage, outgoing_id, with_for_update=True)
                outgoing.meta_message_id = meta_id
                outgoing.error_code = error
                outgoing.status = (
                    "failed" if status == "pending" and outgoing.attempts >= 3 else status
                )
                if outgoing.status in {"failed", "unknown"}:
                    self._notify_delivery(db, outgoing)

        # A local lock after transport success retries only the receipt transaction.
        retry_sqlite_transaction(record_result)
        return True

    def _notify_delivery(self, db, outgoing: OutboundMessage) -> None:
        if outgoing.automation_invitation_id:
            if outgoing.status not in {"blocked_consent", "blocked_window", "failed", "unknown"}:
                return
            notify_offer(
                db,
                db.get(AutomationInvitation, outgoing.automation_invitation_id),
                outgoing.status,
                self.config,
                outgoing.error_code,
            )
        elif outgoing.invitation_id:
            notify_coordinator(
                db,
                db.get(ThirdDayRequest, outgoing.invitation_id),
                outgoing.status,
                self.config,
                outgoing.error_code,
            )

    def recover_sending(self) -> None:
        # Startup recovery is safe under the documented single-worker deployment.
        with self.session_factory.begin() as db:
            for outgoing in db.scalars(
                select(OutboundMessage).where(OutboundMessage.status == "sending").with_for_update()
            ):
                outgoing.status = "unknown"
                outgoing.error_code = "worker_interrupted"
                self._notify_delivery(db, outgoing)

    def run_forever(self, interval: float = 1.0) -> None:
        self.recover_sending()
        while True:
            try:
                progressed = self.maintain()
                progressed = self.process_one() or progressed
                progressed = self.send_one() or progressed
                if not progressed:
                    time.sleep(interval)
            except Exception as exc:
                logger.error("worker_cycle_failed", extra={"error_type": type(exc).__name__})
                time.sleep(5)

    def maintain(self) -> bool:
        if time.monotonic() < self._maintenance_after:
            return False
        try:
            with self.session_factory.begin() as db:
                lock_simulation_transaction(db)
                expired = expire_offers(db, self.config)
                opened = open_monthly_schedules(db, self.config)
            self._maintenance_after = time.monotonic() + 1
            return bool(expired or opened)
        except Exception as exc:
            if is_sqlite_busy(exc):
                return False
            raise


if __name__ == "__main__":
    Worker().run_forever()
