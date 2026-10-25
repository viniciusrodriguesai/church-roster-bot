"""Full exceptional invitation flow, using the production worker without real traffic."""

from datetime import UTC, date, datetime, timedelta
from uuid import uuid4

import httpx
import pytest
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError

from church_roster_bot.administration import conversation
from church_roster_bot.conversations.models import ConversationSession
from church_roster_bot.core.config import Settings
from church_roster_bot.members.models import Volunteer
from church_roster_bot.notifications.invitations import enqueue_invitation
from church_roster_bot.notifications.worker import Worker
from church_roster_bot.scheduling.models import Assignment, ThirdDayRequest
from church_roster_bot.scripts.simulate import receive, simulate
from church_roster_bot.whatsapp.client import MetaSendError
from church_roster_bot.whatsapp.models import InboundEvent, OutboundMessage
from tests.test_phase2 import COORD, FIRST, SECOND, add, bodies, open_service

pytestmark = pytest.mark.parametrize(
    "roster", ["sqlite", pytest.param("postgres", marks=pytest.mark.postgres)], indirect=True
)


class RecordingClient:
    def __init__(self, error=None):
        self.calls = []
        self.error = error

    def send(self, phone, payload):
        self.calls.append((phone, payload))
        if self.error and phone == FIRST:
            raise self.error
        return f"wamid.synthetic-{uuid4().hex}"


def prepare(roster, *, opt_in=True, template="", reason="Necessidade pontual"):
    engine, factory, ids = roster
    member = add(engine, ids)
    start = (date.today().replace(day=1) + timedelta(days=32)).replace(day=1)
    slots = [open_service(roster, start + timedelta(days=n))[1] for n in (0, 3, 6)]
    engine.assign(member, slots[0])
    engine.assign(member, slots[1])
    config = Settings(
        _env_file=None,
        whatsapp_invite_template=template,
        whatsapp_notification_template="aviso_aprovado",
    )
    if opt_in:
        simulate(FIRST, "INICIAR", factory=factory, config=config)
    for message in ("MENU", "7", f"EXCECAO {member} {slots[2]}", reason):
        simulate(COORD, message, factory=factory, config=config)
    return member, slots[2], config


def confirm(factory, config):
    replies = simulate(COORD, "SIM", factory=factory, config=config)
    with factory() as db:
        invitation = db.scalar(select(ThirdDayRequest).order_by(ThirdDayRequest.id.desc()))
        return invitation.id, replies


def outgoing_for(factory, invitation_id):
    with factory() as db:
        return db.scalar(
            select(OutboundMessage).where(OutboundMessage.invitation_id == invitation_id)
        )


def drain(factory, config, client=None):
    client = client or RecordingClient()
    worker = Worker(factory, config, client)
    for _ in range(100):
        progressed = worker.process_one()
        progressed = worker.send_one() or progressed
        if not progressed:
            return client
    pytest.fail("worker did not finish")


def test_confirmation_queues_and_worker_sends_without_volunteer_input(roster):
    engine, factory, _ = roster
    member, slot, config = prepare(roster)
    invitation_id, replies = confirm(factory, config)
    assert "pendente" in bodies(replies)
    outgoing = outgoing_for(factory, invitation_id)
    assert outgoing.status == "pending"
    assert outgoing.payload["type"] == "interactive"
    body = outgoing.payload["interactive"]["body"]["text"]
    assert "19:00" in body and "Ofertas" in body and "Necessidade pontual" in body
    assert f"ACEITAR {invitation_id}" in body and f"RECUSAR {invitation_id}" in body
    assert [b["reply"]["title"] for b in outgoing.payload["interactive"]["action"]["buttons"]] == [
        "ACEITAR",
        "RECUSAR",
    ]
    with factory() as db:
        before = db.scalar(select(func.count(InboundEvent.id)))
    fake = drain(factory, config)
    assert outgoing_for(factory, invitation_id).status == "sent"
    assert sum(phone == FIRST and payload == outgoing.payload for phone, payload in fake.calls) == 1
    with factory() as db:
        assert db.scalar(select(func.count(InboundEvent.id))) == before
        assert db.get(ThirdDayRequest, invitation_id).status == "pending"
        assert db.scalar(select(func.count(Assignment.id))) == 2
        assert db.scalar(select(Assignment.id).where(Assignment.slot_id == slot)) is None
    # Repeating the request and re-enqueueing never creates a second invitation or delivery.
    assert (
        engine.request_third_day(roster[2]["coordinator"], member, slot, "Outra razão")
        == invitation_id
    )
    with factory.begin() as db:
        enqueue_invitation(db, db.get(ThirdDayRequest, invitation_id), config)
    drain(factory, config, fake)
    assert outgoing_for(factory, invitation_id).attempts == 1
    with factory() as db:
        assert db.scalar(select(func.count(ThirdDayRequest.id))) == 1
        with pytest.raises(IntegrityError), db.begin_nested():
            db.add(OutboundMessage(invitation_id=invitation_id, recipient_e164=FIRST, payload={}))
            db.flush()


def test_start_activates_only_the_requesting_user_and_reuses_blocked_delivery(roster):
    engine, factory, ids = roster
    member, _, config = prepare(roster, opt_in=False)
    other = add(engine, ids, SECOND)
    invitation_id, replies = confirm(factory, config)
    assert "bloqueado" in bodies(replies) and "consentimento" in bodies(replies)
    outgoing = outgoing_for(factory, invitation_id)
    assert outgoing.status == "blocked_consent"
    fake = drain(factory, config)
    assert not any(phone == FIRST for phone, _ in fake.calls)
    simulate(FIRST, "INICIAR", factory=factory, config=config)
    assert outgoing_for(factory, invitation_id).id == outgoing.id
    assert outgoing_for(factory, invitation_id).status == "pending"
    drain(factory, config, fake)
    simulate(FIRST, "INICIAR", factory=factory, config=config)
    drain(factory, config, fake)
    assert outgoing_for(factory, invitation_id).attempts == 1
    with factory() as db:
        assert db.get(Volunteer, member).invite_opt_in
        assert not db.get(Volunteer, other).invite_opt_in
        assert not Volunteer.__table__.c.invite_opt_in.default.arg


def test_optout_after_enqueue_blocks_delivery_and_notifies_coordinator(roster):
    _, factory, _ = roster
    _, _, config = prepare(roster)
    invitation_id, _ = confirm(factory, config)
    simulate(FIRST, "PARAR", factory=factory, config=config)
    fake = drain(factory, config)
    assert outgoing_for(factory, invitation_id).status == "blocked_consent"
    assert not any(
        payload.get("type") == "interactive" for phone, payload in fake.calls if phone == FIRST
    )
    assert any(
        "sem consentimento" in str(payload) for phone, payload in fake.calls if phone == COORD
    )


@pytest.mark.parametrize("session_kind", ["missing", "expired"])
@pytest.mark.parametrize("template", ["", "convite_aprovado"])
def test_outside_window_template_carries_all_details_or_blocks(roster, session_kind, template):
    _, factory, _ = roster
    _, _, config = prepare(roster, template=template)
    with factory.begin() as db:
        if session_kind == "missing":
            db.execute(delete(ConversationSession).where(ConversationSession.phone_e164 == FIRST))
        else:
            session = db.scalar(
                select(ConversationSession).where(ConversationSession.phone_e164 == FIRST)
            )
            session.last_inbound_at = datetime.now(UTC) - timedelta(hours=24)
    invitation_id, replies = confirm(factory, config)
    outgoing = outgoing_for(factory, invitation_id)
    if not template:
        assert outgoing.status == "blocked_window"
        assert "bloqueado" in bodies(replies)
        fake = drain(factory, config)
        assert outgoing_for(factory, invitation_id).attempts == 0
        assert not any(
            "Convite excepcional" in str(payload) for phone, payload in fake.calls if phone == FIRST
        )
        return
    assert outgoing.status == "pending"
    template_body = outgoing.payload["template"]
    assert template_body["name"] == template and template_body["language"]["code"] == "pt_BR"
    components = template_body["components"]
    values = [p["text"] for p in components[0]["parameters"]]
    assert values[0] == str(invitation_id)
    assert len(values[1]) == 10 and values[2:] == ["19:00", "Ofertas", "Necessidade pontual"]
    assert [c["parameters"][0]["payload"] for c in components[1:]] == [
        f"ACEITAR {invitation_id}",
        f"RECUSAR {invitation_id}",
    ]
    fake = drain(factory, config)
    assert (FIRST, outgoing.payload) in fake.calls


def test_window_is_rechecked_when_worker_claims_message(roster):
    _, factory, _ = roster
    _, _, config = prepare(roster, template="convite_aprovado")
    invitation_id, _ = confirm(factory, config)
    assert outgoing_for(factory, invitation_id).payload["type"] == "interactive"
    with factory.begin() as db:
        db.scalar(
            select(ConversationSession).where(ConversationSession.phone_e164 == FIRST)
        ).last_inbound_at = datetime.now(UTC) - timedelta(hours=25)
    drain(factory, config)
    assert outgoing_for(factory, invitation_id).payload["type"] == "template"


@pytest.mark.parametrize(
    ("error", "status", "attempts"),
    [
        (MetaSendError(400, "132000"), "failed", 1),
        (MetaSendError(429, "130429"), "failed", 3),
        (httpx.ReadTimeout("ambiguous"), "unknown", 1),
        (MetaSendError(200, "invalid_response"), "unknown", 1),
        (RuntimeError("missing config"), "failed", 1),
    ],
)
def test_failure_or_uncertainty_notifies_coordinator_without_resend(
    roster, error, status, attempts
):
    _, factory, _ = roster
    _, _, config = prepare(roster)
    invitation_id, _ = confirm(factory, config)
    fake = drain(factory, config, RecordingClient(error))
    outgoing = outgoing_for(factory, invitation_id)
    assert (outgoing.status, outgoing.attempts) == (status, attempts)
    key = f"invitation:{invitation_id}:{status}"
    with factory() as db:
        notice = db.scalar(select(OutboundMessage).where(OutboundMessage.dedupe_key == key))
        assert notice.status == "sent"
        assert "Código:" in notice.payload["text"]["body"]
        assert db.scalar(select(func.count(Assignment.id))) == 2
    before = len(fake.calls)
    drain(factory, config, fake)
    assert len(fake.calls) == before


@pytest.mark.parametrize("accept", [True, False])
def test_template_reply_after_session_expiry_notifies_coordinator_exactly_once(roster, accept):
    _, factory, _ = roster
    _, _, config = prepare(roster, template="convite_aprovado")
    invitation_id, _ = confirm(factory, config)
    drain(factory, config)
    command = f"{'ACEITAR' if accept else 'RECUSAR'} {invitation_id}"
    with factory.begin() as db:
        for session in db.scalars(select(ConversationSession)):
            session.last_inbound_at = datetime.now(UTC) - timedelta(hours=25)
            session.expires_at = datetime.now(UTC) - timedelta(hours=1)
        db.add(
            InboundEvent(
                event_id="template-reply",
                kind="message",
                payload={
                    "id": "button-1",
                    "from": FIRST.lstrip("+"),
                    "type": "button",
                    "button": {"payload": command},
                },
            )
        )
    drain(factory, config)
    simulate(FIRST, command, factory=factory, config=config)
    drain(factory, config)
    with factory() as db:
        invitation = db.get(ThirdDayRequest, invitation_id)
        assert invitation.status == ("accepted" if accept else "declined")
        assert invitation.responded_at is not None
        assert db.scalar(select(func.count(Assignment.id))) == (3 if accept else 2)
        notices = db.scalars(
            select(OutboundMessage).where(
                OutboundMessage.dedupe_key
                == f"invitation:{invitation_id}:{'aceito' if accept else 'recusado'}"
            )
        ).all()
        assert len(notices) == 1 and notices[0].status == "sent"
        payload = notices[0].payload
        assert payload["type"] == "template"
        assert payload["template"]["name"] == "aviso_aprovado"
        assert ("aceito" if accept else "recusado") in str(payload)


def test_failed_delivery_webhook_notifies_once_and_never_confirms_assignment(roster):
    _, factory, _ = roster
    _, _, config = prepare(roster)
    invitation_id, _ = confirm(factory, config)
    drain(factory, config)
    meta_id = outgoing_for(factory, invitation_id).meta_message_id
    with factory.begin() as db:
        for index in range(2):
            db.add(
                InboundEvent(
                    event_id=f"failed-{index}",
                    kind="status",
                    payload={"id": meta_id, "status": "failed", "errors": [{"code": 131026}]},
                )
            )
    drain(factory, config)
    with factory() as db:
        assert db.scalar(select(func.count(Assignment.id))) == 2
        assert db.get(ThirdDayRequest, invitation_id).status == "pending"
        assert (
            db.scalar(
                select(func.count(OutboundMessage.id)).where(
                    OutboundMessage.dedupe_key == f"invitation:{invitation_id}:failed"
                )
            )
            == 1
        )
    assert outgoing_for(factory, invitation_id).status == "failed"


def test_inbox_retry_rolls_back_invitation_delivery_and_notice_atomically(roster, monkeypatch):
    _, factory, _ = roster
    _, _, config = prepare(roster)
    event_id = receive(COORD, "SIM", factory=factory)
    worker = Worker(factory, config, RecordingClient())
    original = conversation.enqueue_invitation

    def broken(*args):
        original(*args)
        raise RuntimeError("fail after queueing")

    monkeypatch.setattr(conversation, "enqueue_invitation", broken)
    worker.process_one()
    with factory() as db:
        assert db.scalar(select(func.count(ThirdDayRequest.id))) == 0
        assert (
            db.scalar(
                select(func.count(OutboundMessage.id)).where(
                    OutboundMessage.invitation_id.is_not(None)
                )
            )
            == 0
        )
        assert db.get(InboundEvent, event_id).status == "pending"
    monkeypatch.setattr(conversation, "enqueue_invitation", original)
    drain(factory, config)
    with factory() as db:
        assert db.scalar(select(func.count(ThirdDayRequest.id))) == 1
        assert db.get(InboundEvent, event_id).status == "processed"


def test_interrupted_send_keeps_durable_claim_and_restart_does_not_resend(roster):
    _, factory, _ = roster
    _, _, config = prepare(roster)
    invitation_id, _ = confirm(factory, config)

    class InterruptedClient(RecordingClient):
        def send(self, phone, payload):
            if phone == FIRST and payload.get("type") == "interactive":
                # A separate DB transaction sees the claim before Meta is contacted.
                assert outgoing_for(factory, invitation_id).status == "sending"
                raise SystemExit("crash after external acceptance")
            return super().send(phone, payload)

    worker = Worker(factory, config, InterruptedClient())
    with pytest.raises(SystemExit):
        while worker.send_one():
            pass
    assert outgoing_for(factory, invitation_id).status == "sending"
    worker.recover_sending()
    worker.recover_sending()
    fake = drain(factory, config)
    assert outgoing_for(factory, invitation_id).status == "unknown"
    assert not any(payload.get("type") == "interactive" for _, payload in fake.calls)


def test_answer_before_delivery_cancels_queued_invite(roster):
    _, factory, _ = roster
    _, _, config = prepare(roster)
    invitation_id, _ = confirm(factory, config)
    simulate(FIRST, f"RECUSAR {invitation_id}", factory=factory, config=config)
    drain(factory, config)
    assert outgoing_for(factory, invitation_id).status == "cancelled"


def test_long_justification_is_not_truncated(roster):
    _, factory, _ = roster
    reason = "Motivo " + "á" * 990
    _, _, config = prepare(roster, reason=reason)
    invitation_id, _ = confirm(factory, config)
    outgoing = outgoing_for(factory, invitation_id)
    assert outgoing.payload["type"] == "text"
    assert reason in outgoing.payload["text"]["body"]
    assert f"RECUSAR {invitation_id}" in outgoing.payload["text"]["body"]


def test_acceptance_revalidates_capacity_and_wrong_recipient_cannot_answer(roster):
    engine, factory, ids = roster
    _, slot, config = prepare(roster)
    other = add(engine, ids, SECOND)
    invitation_id, _ = confirm(factory, config)
    simulate(SECOND, f"ACEITAR {invitation_id}", factory=factory, config=config)
    with factory() as db:
        assert db.get(ThirdDayRequest, invitation_id).status == "pending"
    engine.assign(other, slot)
    replies = simulate(FIRST, f"ACEITAR {invitation_id}", factory=factory, config=config)
    assert "não aplicado" in bodies(replies)
    with factory() as db:
        request = db.get(ThirdDayRequest, invitation_id)
        assert request.status == "pending" and request.responded_at is None
        assert (
            db.scalar(select(Assignment.id).where(Assignment.exception_id == invitation_id)) is None
        )
        assert (
            db.scalar(
                select(OutboundMessage.id).where(
                    OutboundMessage.dedupe_key == f"invitation:{invitation_id}:aceito"
                )
            )
            is None
        )


def test_coordinator_can_query_blocked_delivery(roster):
    _, factory, _ = roster
    _, _, config = prepare(roster, opt_in=False)
    invitation_id, _ = confirm(factory, config)
    for message in ("MENU", "7", "LISTAR"):
        replies = simulate(COORD, message, factory=factory, config=config)
    body = bodies(replies)
    assert f"Convite #{invitation_id}" in body and "sem consentimento" in body
