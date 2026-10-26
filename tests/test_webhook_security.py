"""Signed ingress is bounded, validated and acknowledged only after atomic storage."""

import asyncio
import hashlib
import hmac

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, func, select

from church_roster_bot.app import create_app
from church_roster_bot.core.config import Settings
from church_roster_bot.whatsapp.models import InboundEvent
from church_roster_bot.whatsapp.payloads import parse_message
from tests.test_phase2 import meta_envelope, signed_payload


def message(ident="wamid.security", **changes):
    return {"id": ident, "from": "15550100001", "type": "text", "text": {"body": "Oi"}, **changes}


def config(**changes):
    return Settings(_env_file=None, meta_app_secret="test-secret", **changes)


@pytest.mark.parametrize(
    "roster", ["sqlite", pytest.param("postgres", marks=pytest.mark.postgres)], indirect=True
)
class TestWebhookSecurity:
    @pytest.mark.parametrize("raw", [b"{", b"[]", b"\xff", b'{"object": NaN,"entry":[]}'])
    def test_invalid_json_is_rejected_with_a_safe_response(self, roster, raw):
        signature = "sha256=" + hmac.new(b"test-secret", raw, hashlib.sha256).hexdigest()
        response = TestClient(create_app(roster[1], config())).post(
            "/webhooks/whatsapp", content=raw, headers={"x-hub-signature-256": signature}
        )
        assert response.status_code == 400
        assert response.json() == {"detail": "invalid webhook payload"}

    def test_missing_signature_and_tampered_body_do_not_reach_storage(self, roster):
        raw, signature = signed_payload(meta_envelope(message()))
        client = TestClient(create_app(roster[1], config()))
        assert client.post("/webhooks/whatsapp", content=raw).status_code == 403
        assert (
            client.post(
                "/webhooks/whatsapp", content=raw + b" ", headers={"x-hub-signature-256": signature}
            ).status_code
            == 403
        )
        with roster[1]() as db:
            assert db.scalar(select(func.count(InboundEvent.id))) == 0

    def test_non_ascii_signatures_and_verification_tokens_are_rejected(self, roster):
        client = TestClient(
            create_app(roster[1], config(whatsapp_verify_token="synthetic")),
            raise_server_exceptions=False,
        )
        response = client.get(
            "/webhooks/whatsapp",
            params={"hub.mode": "subscribe", "hub.verify_token": "☃", "hub.challenge": "x"},
        )
        assert response.status_code == 403
        response = client.post(
            "/webhooks/whatsapp", content=b"{}", headers=[(b"x-hub-signature-256", b"\xff")]
        )
        assert response.status_code == 403

    @pytest.mark.parametrize("bad", [None, 12, True, [], {}])
    def test_malformed_senders_do_not_enter_the_inbox(self, roster, bad):
        client = TestClient(create_app(roster[1], config()))
        raw, signature = signed_payload(meta_envelope(message(**{"from": bad})))
        assert (
            client.post(
                "/webhooks/whatsapp", content=raw, headers={"x-hub-signature-256": signature}
            ).status_code
            == 400
        )
        with roster[1]() as db:
            assert db.scalar(select(func.count(InboundEvent.id))) == 0

    @pytest.mark.parametrize(
        "value", [{"messages": {}}, {"messages": [None]}, {"statuses": [{"id": "x", "status": []}]}]
    )
    def test_invalid_event_shapes_are_not_silently_acknowledged(self, roster, value):
        payload = meta_envelope(message())
        payload["entry"][0]["changes"][0]["value"] = value
        raw, signature = signed_payload(payload)
        response = TestClient(create_app(roster[1], config())).post(
            "/webhooks/whatsapp", content=raw, headers={"x-hub-signature-256": signature}
        )
        assert response.status_code == 400

    def test_event_budget_rejects_the_entire_batch(self, roster):
        payload = meta_envelope(message())
        payload["entry"][0]["changes"][0]["value"]["messages"] = [
            message(f"wamid.{i}") for i in range(3)
        ]
        raw, signature = signed_payload(payload)
        response = TestClient(create_app(roster[1], config(webhook_max_events=2))).post(
            "/webhooks/whatsapp", content=raw, headers={"x-hub-signature-256": signature}
        )
        assert response.status_code == 413
        with roster[1]() as db:
            assert db.scalar(select(func.count(InboundEvent.id))) == 0

    def test_integrity_failure_is_not_a_duplicate_and_rolls_back_batch(self, roster, caplog):
        factory = roster[1]

        def corrupt_second_event(db, flush_context, instances):
            for obj in db.new:
                if isinstance(obj, InboundEvent) and obj.payload.get("id") == "wamid.bad-db":
                    obj.kind = None

        payload = meta_envelope(message("wamid.good-db"))
        payload["entry"][0]["changes"][0]["value"]["messages"].append(message("wamid.bad-db"))
        raw, signature = signed_payload(payload)
        event.listen(factory.class_, "before_flush", corrupt_second_event)
        try:
            response = TestClient(create_app(factory, config())).post(
                "/webhooks/whatsapp", content=raw, headers={"x-hub-signature-256": signature}
            )
        finally:
            event.remove(factory.class_, "before_flush", corrupt_second_event)
        assert response.status_code == 503
        assert response.json() == {"detail": "event storage unavailable"}
        assert "15550100001" not in caplog.text and "INSERT INTO" not in caplog.text
        with factory() as db:
            assert db.scalar(select(func.count(InboundEvent.id))) == 0

    def test_long_status_identity_does_not_collapse_different_receipts(self, roster):
        payload = meta_envelope(message())
        value = payload["entry"][0]["changes"][0]["value"]
        value.pop("messages")
        value["statuses"] = [
            {"id": "w" * 240, "status": "delivered", "timestamp": "1793000000000001"},
            {"id": "w" * 240, "status": "delivered", "timestamp": "1793000000000002"},
        ]
        raw, signature = signed_payload(payload)
        client = TestClient(create_app(roster[1], config()))
        for expected in (2, 0):
            assert client.post(
                "/webhooks/whatsapp", content=raw, headers={"x-hub-signature-256": signature}
            ).json() == {"accepted": expected}
        with roster[1]() as db:
            assert db.scalar(select(func.count(InboundEvent.id))) == 2

    def test_oversized_stream_stops_before_consuming_tail(self, roster):
        async def run():
            raw = b"x" * 1025
            app = create_app(roster[1], config(webhook_max_bytes=1024))
            signature = "sha256=" + hmac.new(b"test-secret", raw, hashlib.sha256).hexdigest()
            calls = 0

            async def receive():
                nonlocal calls
                calls += 1
                assert calls == 1, "oversized request tail was consumed"
                return {"type": "http.request", "body": raw, "more_body": True}

            output = []

            async def send(value):
                output.append(value)

            await app(
                {
                    "type": "http",
                    "asgi": {"version": "3.0"},
                    "http_version": "1.1",
                    "method": "POST",
                    "scheme": "http",
                    "path": "/webhooks/whatsapp",
                    "raw_path": b"/webhooks/whatsapp",
                    "query_string": b"",
                    "headers": [(b"x-hub-signature-256", signature.encode())],
                    "client": ("127.0.0.1", 1),
                    "server": ("test", 80),
                },
                receive,
                send,
            )
            assert output[0]["status"] == 413

        asyncio.run(run())

    def test_slow_body_has_a_deadline(self, roster):
        async def run():
            app = create_app(roster[1], config(webhook_read_timeout_seconds=0.02))

            async def receive():
                await asyncio.sleep(0.2)
                return {"type": "http.request", "body": b"{}", "more_body": False}

            output = []

            async def send(value):
                output.append(value)

            await app(
                {
                    "type": "http",
                    "asgi": {"version": "3.0"},
                    "http_version": "1.1",
                    "method": "POST",
                    "scheme": "http",
                    "path": "/webhooks/whatsapp",
                    "raw_path": b"/webhooks/whatsapp",
                    "query_string": b"",
                    "headers": [(b"x-hub-signature-256", b"sha256=" + b"0" * 64)],
                    "client": ("127.0.0.1", 1),
                    "server": ("test", 80),
                },
                receive,
                send,
            )
            assert output[0]["status"] == 408

        asyncio.run(run())


@pytest.mark.parametrize("bad", [None, 12, True, [], {}, "+1١١١١١١١١"])
def test_untrusted_phone_types_are_ignored_without_worker_crash(bad):
    assert parse_message(message(**{"from": bad})) is None
