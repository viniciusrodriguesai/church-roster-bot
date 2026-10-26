"""Small HTTP surface: health and authenticated Meta webhooks only."""

import asyncio
import hashlib
import hmac
import json
import logging
import re

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import PlainTextResponse
from pydantic import ValidationError
from sqlalchemy.exc import SQLAlchemyError
from starlette.concurrency import run_in_threadpool
from starlette.requests import ClientDisconnect

from church_roster_bot.core.config import Settings, settings
from church_roster_bot.core.database import SessionLocal
from church_roster_bot.whatsapp.inbox import store_events
from church_roster_bot.whatsapp.payloads import WebhookEnvelope, webhook_events

logger = logging.getLogger(__name__)


def reject_json_constant(value: str):
    raise ValueError("non-finite JSON value")


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
            or not hmac.compare_digest(received.encode(), configured.encode())
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
        signature = request.headers.get("X-Hub-Signature-256", "")
        if not re.fullmatch(r"sha256=[0-9a-f]{64}", signature):
            raise HTTPException(status_code=403, detail="invalid signature")
        length = request.headers.get("content-length")
        if length is not None:
            if not re.fullmatch(r"[0-9]{1,20}", length):
                raise HTTPException(status_code=400, detail="invalid content length")
            if int(length) > config.webhook_max_bytes:
                raise HTTPException(status_code=413, detail="webhook too large")
        raw = bytearray()
        try:
            async with asyncio.timeout(config.webhook_read_timeout_seconds):
                async for chunk in request.stream():
                    if len(raw) + len(chunk) > config.webhook_max_bytes:
                        raise HTTPException(status_code=413, detail="webhook too large")
                    raw.extend(chunk)
        except TimeoutError as exc:
            raise HTTPException(status_code=408, detail="webhook read timed out") from exc
        except ClientDisconnect as exc:
            raise HTTPException(status_code=400, detail="incomplete webhook") from exc
        expected = "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise HTTPException(status_code=403, detail="invalid signature")
        try:
            payload = json.loads(raw, parse_constant=reject_json_constant)
            envelope = WebhookEnvelope.model_validate(payload)
        except (ValidationError, ValueError, UnicodeDecodeError, RecursionError) as exc:
            raise HTTPException(status_code=400, detail="invalid webhook payload") from exc
        if envelope.object != "whatsapp_business_account":
            raise HTTPException(status_code=400, detail="unexpected webhook object")
        try:
            drafts = webhook_events(
                envelope, config.whatsapp_phone_number_id, config.webhook_max_events
            )
        except OverflowError as exc:
            raise HTTPException(status_code=413, detail="too many webhook events") from exc
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="invalid webhook event") from exc
        try:
            accepted = await run_in_threadpool(store_events, session_factory, drafts)
        except SQLAlchemyError as exc:
            logger.error("webhook_storage_failed", extra={"error_type": type(exc).__name__})
            raise HTTPException(status_code=503, detail="event storage unavailable") from exc
        return {"accepted": accepted}

    return app


app = create_app()
