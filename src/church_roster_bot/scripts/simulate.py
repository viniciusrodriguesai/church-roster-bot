"""Local terminal simulator using the same persisted inbox and conversation router."""

import argparse
import uuid

from sqlalchemy import select

from church_roster_bot.core import models  # noqa: F401
from church_roster_bot.core.database import SessionLocal
from church_roster_bot.notifications.worker import Worker
from church_roster_bot.whatsapp.models import InboundEvent, OutboundMessage
from church_roster_bot.whatsapp.payloads import normalize_phone


def simulate(
    phone: str, text: str, *, reply_kind: str = "text", factory=SessionLocal
) -> list[dict]:
    phone = normalize_phone(phone)
    if reply_kind == "text":
        content = {"type": "text", "text": {"body": text}}
    elif reply_kind in {"button_reply", "list_reply"}:
        content = {
            "type": "interactive",
            "interactive": {"type": reply_kind, reply_kind: {"id": text}},
        }
    else:
        raise ValueError("invalid reply kind")
    with factory.begin() as db:
        event = InboundEvent(
            event_id=f"sim-{uuid.uuid4().hex}",
            kind="message",
            payload={
                "id": uuid.uuid4().hex,
                "from": phone.lstrip("+"),
                **content,
            },
        )
        db.add(event)
        db.flush()
        event_id = event.id
        before = (
            db.scalar(select(OutboundMessage.id).order_by(OutboundMessage.id.desc()).limit(1)) or 0
        )
    Worker(session_factory=factory).process_one()
    with factory() as db:
        event = db.get(InboundEvent, event_id)
        if event.status not in {"processed", "ignored"}:
            raise RuntimeError("simulated event was not processed")
        return [
            row.payload
            for row in db.scalars(
                select(OutboundMessage)
                .where(OutboundMessage.id > before, OutboundMessage.recipient_e164 == phone)
                .order_by(OutboundMessage.id)
            )
        ]


def main() -> None:
    parser = argparse.ArgumentParser(description="Simulate WhatsApp without sending messages")
    parser.add_argument("--phone", required=True, help="fictional E.164 number")
    args = parser.parse_args()
    print("Digite uma mensagem. Ctrl+C para sair.")
    try:
        while True:
            text = input("> ")
            for reply in simulate(args.phone, text):
                if reply["type"] == "text":
                    print(reply["text"]["body"])
                else:
                    item = reply["interactive"]
                    print(item["body"]["text"])
                    if item["type"] == "list":
                        for section in item["action"]["sections"]:
                            for row in section["rows"]:
                                print(f"  {row['id']}: {row['title']}")
                    else:
                        for button in item["action"]["buttons"]:
                            print(f"  {button['reply']['id']}: {button['reply']['title']}")
    except (EOFError, KeyboardInterrupt):
        print()


if __name__ == "__main__":
    main()
