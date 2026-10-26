"""Validate the Meta envelope and extract only supported inbound event shapes."""

import hashlib
import json
import re
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, Field


class Change(BaseModel):
    model_config = ConfigDict(extra="allow", strict=True)
    field: str
    value: dict


class Entry(BaseModel):
    model_config = ConfigDict(extra="allow", strict=True)
    changes: list[Change] = Field(max_length=100)


class WebhookEnvelope(BaseModel):
    model_config = ConfigDict(extra="allow", strict=True)
    object: str
    entry: list[Entry] = Field(max_length=100)


@dataclass(frozen=True)
class IncomingMessage:
    message_id: str
    phone_e164: str
    text: str
    reply_kind: str = "text"


def normalize_phone(raw: str) -> str:
    if not isinstance(raw, str):
        raise ValueError("invalid E.164 phone")
    phone = raw if raw.startswith("+") else f"+{raw}"
    if not re.fullmatch(r"\+[1-9][0-9]{7,14}", phone):
        raise ValueError("invalid E.164 phone")
    return phone


def parse_message(value: dict) -> IncomingMessage | None:
    if not isinstance(value, dict) or not isinstance(value.get("id"), str):
        return None
    try:
        phone = normalize_phone(value["from"])
    except (KeyError, TypeError, ValueError):
        return None
    kind = value.get("type")
    if kind == "text":
        content = value.get("text")
        text = content.get("body") if isinstance(content, dict) else None
    elif kind == "interactive":
        interactive = value.get("interactive")
        if not isinstance(interactive, dict):
            return None
        subtype = interactive.get("type")
        text = (
            interactive.get(subtype, {}).get("id")
            if subtype in {"button_reply", "list_reply"}
            and isinstance(interactive.get(subtype), dict)
            else None
        )
    elif kind == "button":
        content = value.get("button")
        text = content.get("payload") if isinstance(content, dict) else None
    else:
        return None
    if not isinstance(text, str) or not text.strip() or len(text) > 4096:
        return None
    return IncomingMessage(value["id"], phone, text.strip(), kind)


@dataclass(frozen=True)
class InboundDraft:
    event_id: str
    kind: str
    payload: dict


def webhook_events(
    envelope: WebhookEnvelope, phone_number_id: str, max_events: int
) -> list[InboundDraft]:
    """Validate the entire bounded batch before opening a storage transaction."""
    drafts = []
    for entry in envelope.entry:
        for change in entry.changes:
            if change.field != "messages":
                continue
            value = change.value
            metadata = value.get("metadata")
            if phone_number_id and (
                not isinstance(metadata, dict) or metadata.get("phone_number_id") != phone_number_id
            ):
                continue
            for kind, key in (("message", "messages"), ("status", "statuses")):
                items = value.get(key, [])
                if not isinstance(items, list):
                    raise ValueError("invalid event collection")
                for item in items:
                    if len(drafts) >= max_events:
                        raise OverflowError("event budget exceeded")
                    if not isinstance(item, dict):
                        raise ValueError("invalid event")
                    ident = item.get("id")
                    if not isinstance(ident, str) or not 1 <= len(ident) <= 1024:
                        raise ValueError("invalid event identity")
                    if kind == "message":
                        normalize_phone(item.get("from"))
                        message_type = item.get("type")
                        if not isinstance(message_type, str) or not 1 <= len(message_type) <= 32:
                            raise ValueError("invalid message type")
                        if message_type in {"text", "interactive", "button"} and not parse_message(
                            item
                        ):
                            raise ValueError("invalid message content")
                    else:
                        status, timestamp = item.get("status"), item.get("timestamp", "")
                        if not isinstance(status, str) or not 1 <= len(status) <= 32:
                            raise ValueError("invalid status")
                        if not isinstance(timestamp, str) or len(timestamp) > 20:
                            raise ValueError("invalid status timestamp")
                        errors = item.get("errors", [])
                        if not isinstance(errors, list) or any(
                            not isinstance(e, dict) for e in errors
                        ):
                            raise ValueError("invalid delivery errors")
                    # Preserve existing short identities. Hash long message IDs
                    # as before, and hash complete status identities rather than
                    # silently truncating distinct timestamps/statuses together.
                    if len(ident) > 240:
                        ident = hashlib.sha256(
                            json.dumps(item, sort_keys=True).encode()
                        ).hexdigest()
                    if kind == "status":
                        ident = f"{ident}:{item['status']}:{item.get('timestamp', '')}"
                    if len(ident) > 255:
                        ident = "status:" + hashlib.sha256(ident.encode()).hexdigest()
                    drafts.append(InboundDraft(ident, kind, item))
    return drafts


def text_payload(body: str) -> dict:
    return {"type": "text", "text": {"body": body[:4096], "preview_url": False}}


def buttons_payload(body: str, options: list[tuple[str, str]]) -> dict:
    if not 1 <= len(options) <= 3 or any(
        len(label) > 20 or len(ident) > 256 for ident, label in options
    ):
        raise ValueError("reply buttons exceed Meta limits")
    return {
        "type": "interactive",
        "interactive": {
            "type": "button",
            "body": {"text": body[:1024]},
            "action": {
                "buttons": [
                    {"type": "reply", "reply": {"id": ident, "title": label}}
                    for ident, label in options
                ]
            },
        },
    }


def list_payload(body: str, options: list[tuple[str, str]], button: str = "Escolher") -> dict:
    if not 1 <= len(options) <= 10 or any(
        len(label) > 24 or len(ident) > 200 for ident, label in options
    ):
        raise ValueError("list rows exceed Meta limits")
    return {
        "type": "interactive",
        "interactive": {
            "type": "list",
            "body": {"text": body[:1024]},
            "action": {
                "button": button[:20],
                "sections": [
                    {
                        "title": "Opções",
                        "rows": [{"id": ident, "title": label} for ident, label in options],
                    }
                ],
            },
        },
    }


def template_payload(
    name: str,
    language: str,
    parameters: list[str] | None = None,
    *,
    quick_replies: list[str] | None = None,
) -> dict:
    template = {"name": name, "language": {"code": language}}
    if parameters:
        template["components"] = [
            {
                "type": "body",
                "parameters": [{"type": "text", "text": " ".join(p.split())} for p in parameters],
            }
        ]
    if quick_replies:
        template.setdefault("components", []).extend(
            {
                "type": "button",
                "sub_type": "quick_reply",
                "index": str(index),
                "parameters": [{"type": "payload", "payload": reply}],
            }
            for index, reply in enumerate(quick_replies)
        )
    return {"type": "template", "template": template}
