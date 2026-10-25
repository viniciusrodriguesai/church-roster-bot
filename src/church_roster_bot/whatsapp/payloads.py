"""Validate the Meta envelope and extract only supported inbound event shapes."""

import re
from dataclasses import dataclass

from pydantic import BaseModel, ConfigDict, Field


class Change(BaseModel):
    model_config = ConfigDict(extra="allow")
    field: str
    value: dict


class Entry(BaseModel):
    model_config = ConfigDict(extra="allow")
    changes: list[Change] = Field(max_length=100)


class WebhookEnvelope(BaseModel):
    model_config = ConfigDict(extra="allow")
    object: str
    entry: list[Entry] = Field(max_length=100)


@dataclass(frozen=True)
class IncomingMessage:
    message_id: str
    phone_e164: str
    text: str
    reply_kind: str = "text"


def normalize_phone(raw: str) -> str:
    phone = raw if raw.startswith("+") else f"+{raw}"
    if not re.fullmatch(r"\+[1-9]\d{7,14}", phone):
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
