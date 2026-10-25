"""Small HTTP surface: health and authenticated Meta webhooks only."""

import hashlib
import hmac
import json

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import PlainTextResponse
from pydantic import ValidationError
from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from church_roster_bot.core.config import Settings, settings
from church_roster_bot.core.database import SessionLocal
from church_roster_bot.whatsapp.models import InboundEvent
from church_roster_bot.whatsapp.payloads import WebhookEnvelope


def create_app(session_factory=SessionLocal, config: Settings = settings) -> FastAPI:
    app = FastAPI(title="Church Roster Bot", docs_url=None, redoc_url=None)

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/webhooks/whatsapp")
    def verify_webhook(request: Request):
        query = request.query_params
        received = query.get("hub.verify_token", "")
        configured = config.whatsapp_verify_token.get_secret_value()
        if (
            not configured
            or query.get("hub.mode") != "subscribe"
            or not hmac.compare_digest(received, configured)
        ):
            raise HTTPException(status_code=403, detail="verification failed")
        challenge = query.get("hub.challenge")
        if challenge is None:
            raise HTTPException(status_code=400, detail="missing challenge")
        return PlainTextResponse(challenge)

    @app.post("/webhooks/whatsapp")
    async def receive_webhook(request: Request):
        secret = config.meta_app_secret.get_secret_value()
        if not secret:
            raise HTTPException(status_code=503, detail="webhook secret is not configured")
        raw = await request.body()
        if len(raw) > config.webhook_max_bytes:
            raise HTTPException(status_code=413, detail="webhook too large")
        signature = request.headers.get("X-Hub-Signature-256", "")
        expected = "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise HTTPException(status_code=403, detail="invalid signature")
        try:
            envelope = WebhookEnvelope.model_validate_json(raw)
        except ValidationError as exc:
            raise HTTPException(status_code=400, detail="invalid webhook payload") from exc
        if envelope.object != "whatsapp_business_account":
            raise HTTPException(status_code=400, detail="unexpected webhook object")
        accepted = 0
        try:
            with session_factory.begin() as db:
                for entry in envelope.entry:
                    for change in entry.changes:
                        if change.field != "messages":
                            continue
                        value = change.value
                        metadata = value.get("metadata")
                        if config.whatsapp_phone_number_id and (
                            not isinstance(metadata, dict)
                            or metadata.get("phone_number_id") != config.whatsapp_phone_number_id
                        ):
                            continue
                        for kind, items in (
                            ("message", value.get("messages", [])),
                            ("status", value.get("statuses", [])),
                        ):
                            if not isinstance(items, list):
                                continue
                            for item in items:
                                if not isinstance(item, dict):
                                    continue
                                ident = item.get("id")
                                if not isinstance(ident, str) or len(ident) > 240:
                                    ident = hashlib.sha256(
                                        json.dumps(item, sort_keys=True).encode()
                                    ).hexdigest()
                                if kind == "status":
                                    ident = (
                                        f"{ident}:{item.get('status', '')}:"
                                        f"{item.get('timestamp', '')}"
                                    )
                                try:
                                    with db.begin_nested():
                                        db.add(
                                            InboundEvent(
                                                event_id=ident[:255], kind=kind, payload=item
                                            )
                                        )
                                        db.flush()
                                    accepted += 1
                                except IntegrityError:
                                    pass
        except SQLAlchemyError as exc:
            raise HTTPException(status_code=503, detail="event storage unavailable") from exc
        return {"accepted": accepted}

    return app


app = create_app()
