"""Recoverable infrastructure failures and safe user/transport error boundaries."""

from datetime import UTC, datetime, timedelta

import httpx
import pytest
from pydantic import ValidationError
from sqlalchemy import event, func, select
from sqlalchemy.exc import OperationalError

from church_roster_bot.conversations.models import ConversationSession
from church_roster_bot.core.config import Settings
from church_roster_bot.notifications.outbox import window_open
from church_roster_bot.notifications.worker import Worker
from church_roster_bot.scripts.simulate import receive, simulate
from church_roster_bot.whatsapp.client import MetaSendError, WhatsAppClient
from church_roster_bot.whatsapp.models import InboundEvent, OutboundMessage
from church_roster_bot.whatsapp.payloads import text_payload
from tests.test_invitation_delivery import RecordingClient
from tests.test_phase2 import COORD, FIRST, add, bodies


@pytest.mark.parametrize(
    "roster", ["sqlite_file", pytest.param("postgres", marks=pytest.mark.postgres)], indirect=True
)
class TestFailureRecovery:
    def test_transient_database_failure_rolls_back_without_consuming_event_attempt(self, roster):
        engine, factory, ids = roster
        add(engine, ids)
        event_id = receive(FIRST, "Oi", factory=factory)
        worker = Worker(factory, Settings(_env_file=None), RecordingClient())
        original = worker.bot.handle

        def interrupted(db, message):
            original(db, message)
            db.flush()
            raise OperationalError("synthetic interruption", {}, RuntimeError("private details"))

        worker.bot.handle = interrupted
        with pytest.raises(OperationalError):
            worker.process_one()
        with factory() as db:
            assert (
                db.get(InboundEvent, event_id).status,
                db.get(InboundEvent, event_id).attempts,
            ) == ("pending", 0)
            assert db.scalar(select(func.count(OutboundMessage.id))) == 0
        worker.bot.handle = original
        assert worker.process_one()
        with factory() as db:
            assert db.get(InboundEvent, event_id).attempts == 1
            assert db.scalar(select(func.count(OutboundMessage.id))) == 1

    def test_startup_database_failure_retries_recovery_before_processing(self, roster, monkeypatch):
        factory = roster[1]
        with factory.begin() as db:
            db.add(
                OutboundMessage(
                    recipient_e164=FIRST,
                    payload=text_payload("Fictitious"),
                    status="sending",
                    attempts=1,
                )
            )
        worker = Worker(factory, Settings(_env_file=None), RecordingClient())
        original = worker.recover_sending
        calls, sleeps = [], []

        def recovery():
            calls.append("recover")
            if len(calls) == 1:
                raise OperationalError("synthetic outage", {}, RuntimeError("private"))
            original()

        class StopLoop(BaseException):
            pass

        def stop():
            assert len(calls) == 2
            raise StopLoop()

        monkeypatch.setattr(worker, "recover_sending", recovery)
        monkeypatch.setattr(worker, "send_one", stop)
        monkeypatch.setattr("church_roster_bot.notifications.worker.time.sleep", sleeps.append)
        with pytest.raises(StopLoop):
            worker.run_forever()
        assert sleeps == [5]
        with factory() as db:
            outgoing = db.scalar(select(OutboundMessage))
            assert (outgoing.status, outgoing.attempts) == ("unknown", 1)

    @pytest.mark.parametrize(
        "current,receipt,expected",
        [
            ("read", "failed", "read"),
            ("delivered", "failed", "delivered"),
            ("failed", "sent", "failed"),
            ("failed", "read", "read"),
            ("sent", "failed", "failed"),
        ],
    )
    def test_receipts_do_not_regress_confirmed_delivery(self, roster, current, receipt, expected):
        factory = roster[1]
        with factory.begin() as db:
            db.add(
                OutboundMessage(
                    recipient_e164=FIRST,
                    payload=text_payload("Fictitious"),
                    meta_message_id="wamid.safe-order",
                    status=current,
                )
            )
            db.add(
                InboundEvent(
                    event_id="fictitious-receipt",
                    kind="status",
                    payload={
                        "id": "wamid.safe-order",
                        "status": receipt,
                        "errors": [{"code": 131047}],
                    },
                )
            )
        assert Worker(factory, Settings(_env_file=None), RecordingClient()).process_one()
        with factory() as db:
            assert db.scalar(select(OutboundMessage.status)) == expected

    def test_error_code_cannot_copy_a_provider_payload_into_notifications(self, roster):
        factory = roster[1]
        with factory.begin() as db:
            db.add(
                OutboundMessage(
                    recipient_e164=FIRST,
                    payload=text_payload("Fictitious"),
                    meta_message_id="wamid.bad-code",
                    status="sent",
                )
            )
            db.add(
                InboundEvent(
                    event_id="bad-code",
                    kind="status",
                    payload={
                        "id": "wamid.bad-code",
                        "status": "failed",
                        "errors": [{"code": {"PRIVATE_TOKEN": "FICTITIOUS_SECRET"}}],
                    },
                )
            )
        Worker(factory, Settings(_env_file=None), RecordingClient()).process_one()
        with factory() as db:
            assert db.scalar(select(OutboundMessage.error_code)) == "unknown"

    def test_unexpected_transport_runtime_error_is_ambiguous_and_not_resent(self, roster, caplog):
        engine, factory, ids = roster
        add(engine, ids)
        simulate(FIRST, "Oi", factory=factory)
        client = RecordingClient(RuntimeError("private transport implementation failure"))
        worker = Worker(factory, Settings(_env_file=None), client)
        assert worker.send_one()
        worker.recover_sending()
        assert not worker.send_one()
        with factory() as db:
            row = db.scalar(select(OutboundMessage))
            assert (row.status, row.attempts) == ("unknown", 1)
        assert len(client.calls) == 1
        assert "outbound_attempt_failed" in caplog.text
        assert "private transport implementation failure" not in caplog.text

    def test_database_failure_after_send_preserves_claim_and_restart_never_resends(self, roster):
        engine, factory, ids = roster
        add(engine, ids)
        simulate(FIRST, "Oi", factory=factory)
        client = RecordingClient()
        worker = Worker(factory, Settings(_env_file=None), client)

        def fail_receipt(conn, cursor, statement, parameters, context, executemany):
            if statement.startswith("UPDATE outbound_messages") and "meta_message_id" in statement:
                raise OperationalError("synthetic outage", {}, RuntimeError("private details"))

        bind = factory.kw["bind"]
        event.listen(bind, "before_cursor_execute", fail_receipt)
        try:
            with pytest.raises(OperationalError):
                worker.send_one()
        finally:
            event.remove(bind, "before_cursor_execute", fail_receipt)
        with factory() as db:
            row = db.scalar(select(OutboundMessage))
            assert (row.status, row.attempts, row.meta_message_id) == ("sending", 1, None)
        restarted = Worker(factory, Settings(_env_file=None), client)
        restarted.recover_sending()
        assert not restarted.send_one()
        assert len(client.calls) == 1
        with factory() as db:
            assert db.scalar(select(OutboundMessage.status)) == "unknown"

    def test_future_inbound_timestamp_does_not_open_a_free_form_window(self, roster):
        engine, factory, ids = roster
        add(engine, ids)
        simulate(FIRST, "Oi", factory=factory)
        with factory.begin() as db:
            session = db.scalar(select(ConversationSession))
            session.last_inbound_at = datetime.now(UTC) + timedelta(days=1)
        with factory() as db:
            assert not window_open(db, FIRST)

    @pytest.mark.parametrize(
        "command", ["EXCECAO FICTITIOUS_SECRET 1", "SUBSTITUIR 1);DROP_TABLE 1"]
    )
    def test_invalid_administrative_ids_do_not_expose_parser_details(self, roster, command):
        factory = roster[1]
        simulate(COORD, "MENU", factory=factory)
        simulate(COORD, "7", factory=factory)
        response = bodies(simulate(COORD, command, factory=factory))
        assert "invalid literal" not in response and "FICTITIOUS_SECRET" not in response
        assert "DROP_TABLE" not in response

    @pytest.mark.parametrize(
        "command", ["ACEITAR " + str(2**100), "ESPERA " + str(2**100), "ACEITAR A" + str(2**100)]
    )
    def test_oversized_ids_return_safe_reply_and_process_once(self, roster, command):
        engine, factory, ids = roster
        add(engine, ids)
        simulate(FIRST, "Oi", factory=factory)
        response = bodies(simulate(FIRST, command, factory=factory))
        assert response and "OverflowError" not in response and "out of range" not in response
        with factory() as db:
            assert all(
                e.status == "processed" and e.attempts == 1
                for e in db.scalars(select(InboundEvent))
            )


@pytest.mark.parametrize(
    "body", [None, [], {"error": []}, {"error": {"code": "FICTITIOUS_SECRET"}}]
)
def test_malformed_http_errors_preserve_status_without_copying_provider_data(body):
    client = WhatsAppClient(
        Settings(
            _env_file=None, whatsapp_access_token="fictitious", whatsapp_phone_number_id="123"
        ),
        transport=httpx.MockTransport(lambda _: httpx.Response(429, json=body)),
    )
    with pytest.raises(MetaSendError) as caught:
        client.send(FIRST, text_payload("Fictitious"))
    assert (caught.value.status_code, caught.value.code) == (429, "unknown")


@pytest.mark.parametrize("ident", [None, 0, {}, [], "", "x" * 256])
def test_malformed_success_identity_is_ambiguous(ident):
    client = WhatsAppClient(
        Settings(
            _env_file=None, whatsapp_access_token="fictitious", whatsapp_phone_number_id="123"
        ),
        transport=httpx.MockTransport(
            lambda _: httpx.Response(200, json={"messages": [{"id": ident}]})
        ),
    )
    with pytest.raises(MetaSendError) as caught:
        client.send(FIRST, text_payload("Fictitious"))
    assert (caught.value.status_code, caught.value.code) == (200, "invalid_response")


def test_settings_repr_and_validation_errors_do_not_print_sensitive_inputs():
    secret = "FICTITIOUS_PRIVATE_CONFIGURATION"
    assert secret not in repr(
        Settings(_env_file=None, database_url=f"postgresql://user:{secret}@localhost/db")
    )
    with pytest.raises(ValidationError) as caught:
        Settings(_env_file=None, whatsapp_api_version=secret)
    assert secret not in str(caught.value)
