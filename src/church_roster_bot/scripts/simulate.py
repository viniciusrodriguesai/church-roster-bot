"""Local terminal simulator using the same persisted inbox and conversation router."""

import argparse
import threading
import uuid

from sqlalchemy import select

from church_roster_bot.core import models  # noqa: F401
from church_roster_bot.core.config import Settings
from church_roster_bot.core.database import SessionLocal
from church_roster_bot.notifications.worker import Worker
from church_roster_bot.whatsapp.models import InboundEvent, OutboundMessage
from church_roster_bot.whatsapp.payloads import normalize_phone


def receive(phone: str, text: str, *, reply_kind="text", factory=SessionLocal) -> int:
    """Persist a synthetic incoming event without invoking the network."""
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
            payload={"id": uuid.uuid4().hex, "from": phone.lstrip("+"), **content},
        )
        db.add(event)
        db.flush()
        return event.id


def simulate(
    phone: str, text: str, *, reply_kind: str = "text", factory=SessionLocal, config=None
) -> list[dict]:
    phone = normalize_phone(phone)
    with factory() as db:
        before = (
            db.scalar(select(OutboundMessage.id).order_by(OutboundMessage.id.desc()).limit(1)) or 0
        )
    event_id = receive(phone, text, reply_kind=reply_kind, factory=factory)
    worker = Worker(session_factory=factory, **({"config": config} if config else {}))
    while worker.process_one():
        with factory() as db:
            if db.get(InboundEvent, event_id).status in {"processed", "ignored", "failed"}:
                break
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


class SimulationClient:
    def send(self, recipient: str, payload: dict) -> str:
        return f"sim-out-{uuid.uuid4().hex}"


class TerminalInbox:
    """Track receipts by id, including older pending rows that are delivered later."""

    def __init__(self, phone: str, factory=SessionLocal):
        self.phone = normalize_phone(phone)
        self.factory = factory
        with factory() as db:
            self.seen = set(db.scalars(self._query()))

    def _query(self):
        return select(OutboundMessage.id).where(
            OutboundMessage.recipient_e164 == self.phone,
            OutboundMessage.status.in_(("sent", "delivered", "read")),
        )

    def poll(self) -> list[dict]:
        with self.factory() as db:
            rows = db.scalars(
                select(OutboundMessage)
                .where(OutboundMessage.id.in_(self._query()))
                .order_by(OutboundMessage.id)
            ).all()
            replies = [row.payload for row in rows if row.id not in self.seen]
            self.seen.update(row.id for row in rows)
            return replies


def render_reply(reply: dict) -> str:
    if reply["type"] == "text":
        return reply["text"]["body"]
    if reply["type"] == "template":
        template = reply["template"]
        lines = [f"[Template fictício: {template['name']}]"]
        for component in template.get("components", []):
            for parameter in component.get("parameters", []):
                lines.append(parameter.get("text", parameter.get("payload", "")))
        return "\n".join(lines)
    item = reply["interactive"]
    lines = [item["body"]["text"]]
    if item["type"] == "list":
        for section in item["action"]["sections"]:
            lines.extend(f"  {row['id']}: {row['title']}" for row in section["rows"])
    else:
        lines.extend(
            f"  {button['reply']['id']}: {button['reply']['title']}"
            for button in item["action"]["buttons"]
        )
    return "\n".join(lines)


def run_terminal(
    phone: str, *, factory=SessionLocal, read=input, write=print, interval=0.2
) -> None:
    phone = normalize_phone(phone)
    # Explicit local transport and template names: never use Meta credentials in the simulator.
    config = Settings(
        whatsapp_invite_template="simulacao_convite",
        whatsapp_notification_template="simulacao_notificacao",
    )
    worker = Worker(session_factory=factory, config=config, client=SimulationClient())
    inbox = TerminalInbox(phone, factory)
    stopped = threading.Event()
    errors: list[Exception] = []

    def deliver():
        try:
            while not stopped.is_set():
                worker.process_one()
                for _ in range(100):
                    if not worker.send_one():
                        break
                for reply in inbox.poll():
                    write("\n" + render_reply(reply))
                stopped.wait(interval)
        except Exception as exc:
            errors.append(exc)
            write(f"\nSimulador interrompido: {type(exc).__name__}. Pressione Enter para sair.")
            stopped.set()

    delivery = threading.Thread(target=deliver, daemon=True)
    delivery.start()
    try:
        # Keep stdin on the main thread so Ctrl+C/EOF can stop the worker cleanly.
        while not stopped.is_set():
            text = read("> ")
            if text:
                receive(phone, text, factory=factory)
    except EOFError:
        pass
    finally:
        stopped.set()
        delivery.join(timeout=5)
    if errors:
        raise errors[0]


def main() -> None:
    parser = argparse.ArgumentParser(description="Simulate WhatsApp without sending messages")
    parser.add_argument("--phone", required=True, help="fictional E.164 number")
    args = parser.parse_args()
    print("Digite uma mensagem. Ctrl+C para sair.")
    try:
        run_terminal(args.phone)
    except (EOFError, KeyboardInterrupt):
        print()


if __name__ == "__main__":
    main()
