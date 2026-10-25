"""Messages arrive while the terminal's text input is still waiting."""

import threading
from queue import Queue

import pytest

from church_roster_bot.scripts.simulate import TerminalInbox, receive, render_reply, run_terminal
from church_roster_bot.whatsapp.models import OutboundMessage
from church_roster_bot.whatsapp.payloads import template_payload, text_payload
from tests.test_invitation_delivery import prepare
from tests.test_phase2 import COORD, FIRST


@pytest.mark.parametrize("roster", ["sqlite_file"], indirect=True)
def test_idle_terminal_delivers_invitation_without_any_volunteer_input(roster):
    _, factory, _ = roster
    prepare(roster)
    waiting, delivered = threading.Event(), threading.Event()
    input_queue = Queue()
    output, errors, reads = [], [], []

    def read(_prompt):
        reads.append(True)
        waiting.set()
        value = input_queue.get(timeout=15)
        if value is None:
            raise EOFError
        return value

    def write(value):
        output.append(value)
        if "Convite excepcional" in value:
            delivered.set()

    def terminal():
        try:
            run_terminal(FIRST, factory=factory, read=read, write=write, interval=0.01)
        except Exception as exc:
            errors.append(exc)

    thread = threading.Thread(target=terminal, daemon=True)
    thread.start()
    try:
        assert waiting.wait(5)
        # The coordinator confirms in another terminal. The volunteer never presses Enter.
        receive(COORD, "SIM", factory=factory)
        assert delivered.wait(10), (output, errors)
        assert len(reads) == 1
        assert "ACEITAR" in "\n".join(output) and "RECUSAR" in "\n".join(output)
        assert not errors
    finally:
        input_queue.put(None)
        thread.join(5)
    assert not thread.is_alive()


def test_terminal_poll_does_not_skip_older_pending_messages(roster):
    _, factory, _ = roster
    inbox = TerminalInbox(FIRST, factory)
    with factory.begin() as db:
        older = OutboundMessage(recipient_e164=FIRST, payload=text_payload("convite"))
        newer = OutboundMessage(
            recipient_e164=FIRST, payload=text_payload("resposta"), status="sent"
        )
        db.add_all([older, newer])
        db.flush()
        older_id = older.id
    assert inbox.poll() == [text_payload("resposta")]
    with factory.begin() as db:
        db.get(OutboundMessage, older_id).status = "sent"
    assert inbox.poll() == [text_payload("convite")]
    assert inbox.poll() == []


def test_terminal_displays_template_parameters_and_response_commands():
    reply = template_payload(
        "simulacao_convite",
        "pt_BR",
        ["1", "01/11/2026", "19:00", "Ofertas", "Necessidade"],
        quick_replies=["ACEITAR 1", "RECUSAR 1"],
    )
    rendered = render_reply(reply)
    for detail in ("01/11/2026", "19:00", "Ofertas", "Necessidade", "ACEITAR 1", "RECUSAR 1"):
        assert detail in rendered
