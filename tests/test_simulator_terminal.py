"""Messages arrive while the terminal's text input is still waiting."""

import threading
from queue import Queue

import pytest
from sqlalchemy import func, select

from church_roster_bot.notifications.worker import Worker
from church_roster_bot.scheduling.models import Assignment, ThirdDayRequest
from church_roster_bot.scripts.simulate import (
    TerminalInbox,
    receive,
    render_reply,
    render_terminal_reply,
    run_terminal,
    terminal_input,
)
from church_roster_bot.whatsapp.models import InboundEvent, OutboundMessage
from church_roster_bot.whatsapp.payloads import template_payload, text_payload
from tests.test_invitation_delivery import RecordingClient, drain, prepare
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


def test_terminal_receipts_do_not_repeat_after_status_changes_or_restart(roster):
    _, factory, _ = roster
    inbox = TerminalInbox(FIRST, factory)
    with factory.begin() as db:
        message = OutboundMessage(
            recipient_e164=FIRST, payload=text_payload("aceite"), status="sent", attempts=1
        )
        db.add_all(
            [
                message,
                OutboundMessage(
                    recipient_e164=COORD,
                    payload=text_payload("outro destinatário"),
                    status="sent",
                    attempts=1,
                ),
            ]
        )
        db.flush()
        message_id = message.id
    assert inbox.poll() == [text_payload("aceite")]
    restarted = TerminalInbox(FIRST, factory)
    for status in ("delivered", "read"):
        with factory.begin() as db:
            db.get(OutboundMessage, message_id).status = status
        assert inbox.poll() == []
        assert restarted.poll() == []
    with factory() as db:
        assert db.get(OutboundMessage, message_id).attempts == 1


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


@pytest.mark.parametrize(
    "message",
    [
        "Oi",
        "MENU",
        "1",
        "S12",
        "R5",
        "SIM",
        "ACEITAR 1",
        "RECUSAR 1",
        "EXCECAO 5 5",
        "Motivo: necessidade de cobertura.",
        "Administração precisa de cobertura.",
        "2026-11-01 2026-11-02",
    ],
)
def test_terminal_preserves_commands_and_free_text(message):
    assert terminal_input(f"  {message}  ") == message


def test_every_rendered_line_is_identified_and_cannot_become_input():
    rendered = render_terminal_reply(text_payload("Convite aceito.\n\nACEITAR 1\nMENU"))
    assert all(line.startswith("[bot] ") for line in rendered.splitlines())
    assert terminal_input(rendered) is None
    assert all(terminal_input(line) is None for line in rendered.splitlines())


@pytest.mark.parametrize(
    "roster", ["sqlite_file", pytest.param("postgres", marks=pytest.mark.postgres)], indirect=True
)
def test_two_terminals_accept_once_and_ignore_pasted_transcript(roster, monkeypatch):
    _, factory, _ = roster
    member, slot, config = prepare(roster)
    client = drain(factory, config)
    # Both terminals run the real worker, sharing only a local recording transport.
    monkeypatch.setattr("church_roster_bot.scripts.simulate.SimulationClient", lambda: client)
    condition = threading.Condition()
    inputs = {phone: Queue() for phone in (FIRST, COORD)}
    output = {phone: [] for phone in inputs}
    waiting = dict.fromkeys(inputs, 0)
    polls = dict.fromkeys(inputs, 0)
    errors = []
    original_poll = TerminalInbox.poll

    def poll(inbox):
        replies = original_poll(inbox)
        with condition:
            polls[inbox.phone] += 1
            condition.notify_all()
        return replies

    monkeypatch.setattr(TerminalInbox, "poll", poll)

    def terminal(phone):
        def read(_prompt):
            with condition:
                waiting[phone] += 1
                condition.notify_all()
            value = inputs[phone].get(timeout=20)
            if value is None:
                raise EOFError
            return value

        def write(value):
            with condition:
                output[phone].append(value)
                condition.notify_all()

        try:
            run_terminal(phone, factory=factory, read=read, write=write, interval=0.01)
        except Exception as exc:
            with condition:
                errors.append(exc)
                condition.notify_all()

    def wait_for(predicate):
        with condition:
            assert condition.wait_for(lambda: errors or predicate(), timeout=15), output
            assert not errors

    def quiet_cycles():
        with condition:
            before = polls.copy()
        wait_for(lambda: all(polls[p] >= before[p] + 5 for p in polls))

    def snapshot():
        with factory() as db:
            return (
                list(
                    db.execute(
                        select(
                            InboundEvent.id, InboundEvent.status, InboundEvent.attempts
                        ).order_by(InboundEvent.id)
                    )
                ),
                list(
                    db.execute(
                        select(
                            OutboundMessage.id, OutboundMessage.status, OutboundMessage.attempts
                        ).order_by(OutboundMessage.id)
                    )
                ),
                len(client.calls),
            )

    threads = [threading.Thread(target=terminal, args=(p,), daemon=True) for p in inputs]
    for thread in threads:
        thread.start()
    try:
        wait_for(lambda: all(waiting.values()))
        inputs[COORD].put("SIM")
        wait_for(lambda: "Convite excepcional" in "\n".join(output[FIRST]))
        with factory() as db:
            invitation_id = db.scalar(select(ThirdDayRequest.id))
        inputs[FIRST].put(f"ACEITAR {invitation_id}")
        wait_for(lambda: "inscrição confirmada" in "\n".join(output[FIRST]))
        wait_for(lambda: "aceito." in "\n".join(output[COORD]))
        quiet_cycles()
        before = snapshot()
        displayed = {p: list(lines) for p, lines in output.items()}
        # Reproduce the legacy transcript found in the demo inbox after ACEITAR 1.
        transcript = [
            r" C:\project> .\.venv\Scripts\python.exe -m church_roster_bot.scripts.simulate",
            "Digite uma mensagem. Ctrl+C para sair.",
            "> MENU",
            ">",
            "ADMINISTRAÇÃO — DIACONATO",
            "ADMINISTRA��O � DIACONATO",
            "Escolha uma opção:",
            "Escolha uma op��o:",
            "  1: Gerenciar diáconos",
            "  9: Participantes",
            "ESCALAS DA IGREJA",
            "Olá, Pessoa Exemplo!",
            "",
            "   ",
            # Newly rendered output must also be safe to paste into either terminal.
            *"\n".join(displayed[FIRST] + displayed[COORD]).splitlines(),
            "MENU\nSIM",  # A multiline paste is never a sequence of user commands.
        ]
        reads_before = waiting[FIRST]
        for line in transcript:
            inputs[FIRST].put(line)
        wait_for(lambda: waiting[FIRST] >= reads_before + len(transcript))
        quiet_cycles()
        assert snapshot() == before
        assert output == displayed
        # A new explicit duplicate response must not duplicate the assignment or notice.
        inputs[FIRST].put(f"ACEITAR {invitation_id}")
        wait_for(lambda: "already answered" in "\n".join(output[FIRST]))
        quiet_cycles()
        with factory() as db:
            invitation = db.get(ThirdDayRequest, invitation_id)
            assert (invitation.status, invitation.volunteer_id, invitation.slot_id) == (
                "accepted",
                member,
                slot,
            )
            assignments = db.scalars(
                select(Assignment).where(Assignment.exception_id == invitation_id)
            ).all()
            assert len(assignments) == 1 and assignments[0].status == "confirmed"
            assert db.scalar(select(func.count(Assignment.id))) == 3
            notice = db.scalars(
                select(OutboundMessage).where(
                    OutboundMessage.dedupe_key == f"invitation:{invitation_id}:aceito"
                )
            ).one()
            assert (notice.status, notice.attempts) == ("sent", 1)
            assert all(
                status == "processed" and attempts == 1
                for status, attempts in db.execute(
                    select(InboundEvent.status, InboundEvent.attempts)
                )
            )
            assert all(
                status == "sent" and attempts == 1
                for status, attempts in db.execute(
                    select(OutboundMessage.status, OutboundMessage.attempts)
                )
            )
        assert sum("aceito." in line for line in output[COORD]) == 1
        assert not any("ESCALAS DA IGREJA" in line for line in output[FIRST])
        # A fresh worker and repeated idle polls must leave every receipt unchanged.
        worker = Worker(factory, config, RecordingClient())
        before = snapshot()
        for _ in range(5):
            assert not worker.process_one()
            assert not worker.send_one()
        assert snapshot() == before
    finally:
        for queue in inputs.values():
            queue.put(None)
        for thread in threads:
            thread.join(5)
    assert not any(thread.is_alive() for thread in threads)
    assert not errors
