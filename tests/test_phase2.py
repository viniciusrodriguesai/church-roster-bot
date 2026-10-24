"""Conversation, webhook and transport integration without real WhatsApp traffic."""

import hashlib
import hmac
import json
from datetime import UTC, date, datetime, timedelta

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from church_roster_bot.app import create_app
from church_roster_bot.audit.models import ScheduleVersion
from church_roster_bot.conversations.models import ConversationSession
from church_roster_bot.core.config import Settings
from church_roster_bot.members.models import Coordinator, GeneralAdmin, Volunteer, VolunteerRole
from church_roster_bot.members.schemas import VolunteerCreate
from church_roster_bot.ministries.models import Ministry, Role
from church_roster_bot.notifications.worker import Worker
from church_roster_bot.scheduling.errors import NoCapacity, RosterError
from church_roster_bot.scheduling.lifecycle import (
    format_schedule,
    manual_replace,
    publish_schedule,
    request_replacement,
    schedule_for,
    set_slot_capacity,
    slot_state,
    transition_assignment,
    transition_schedule,
)
from church_roster_bot.scheduling.models import (
    Assignment,
    MonthlySchedule,
    ReplacementRequest,
    ThirdDayRequest,
)
from church_roster_bot.scripts.simulate import simulate
from church_roster_bot.services.models import Slot, WorshipService
from church_roster_bot.whatsapp.client import MetaSendError, WhatsAppClient
from church_roster_bot.whatsapp.models import InboundEvent, OutboundMessage
from church_roster_bot.whatsapp.payloads import parse_message, text_payload
from tests.conftest import make_service

COORD = "+15550100000"
FIRST = "+15550100001"
SECOND = "+15550100002"


def send(factory, phone, value):
    return simulate(phone, value, factory=factory)


def bodies(replies):
    return "\n".join(
        r["text"]["body"] if r["type"] == "text" else r["interactive"]["body"]["text"]
        for r in replies
    )


def next_day(days=3):
    return date.today() + timedelta(days=days)


def add(engine, ids, phone=FIRST, roles=None):
    return engine.register_volunteer(
        ids["church"],
        VolunteerCreate(
            name="Pessoa Exemplo", phone_e164=phone, role_ids=set(roles or [ids["offer"]])
        ),
    )


def open_service(roster, day=None, capacity=1):
    engine, factory, ids = roster
    day = day or next_day()
    service_id, slots = make_service(
        engine, factory, ids["ministry"], day, {ids["offer"]: capacity}
    )
    with factory.begin() as db:
        schedule = schedule_for(db, ids["ministry"], day, create=True)
        if schedule.state == "draft":
            transition_schedule(schedule, "open")
    return service_id, slots[ids["offer"]]


def test_volunteer_full_signup_and_persistent_restart(roster):
    engine, factory, ids = roster
    member = add(engine, ids)
    service_id, slot_id = open_service(roster)
    assert "ESCALAS DA IGREJA" in bodies(send(factory, FIRST, "Oi"))
    assert "Escolha um culto" in bodies(send(factory, FIRST, "1"))
    assert "função autorizada" in bodies(send(factory, FIRST, str(service_id)))
    assert "Confirmar" in bodies(send(factory, FIRST, str(slot_id)))
    assert "CONFIRMADA" in bodies(send(factory, FIRST, "SIM"))
    with factory() as db:
        assert db.scalar(select(func.count(Assignment.id))) == 1
        assert db.scalar(select(Assignment.volunteer_id)) == member
        assert db.scalar(select(ConversationSession.state)) == "menu"
    # A new worker/process can read the committed conversation and assignment.
    assert "Pessoa Exemplo" not in bodies(send(factory, FIRST, "2"))
    assert "Ofertas" in bodies(send(factory, FIRST, "2"))


def test_coordinator_registers_member_with_confirmation_and_edits_roles(roster):
    _, factory, ids = roster
    send(factory, COORD, "MENU")
    send(factory, COORD, "1")
    send(factory, COORD, "NOVO")
    send(factory, COORD, "Ana Exemplo")
    send(factory, COORD, SECOND)
    send(factory, COORD, str(ids["offer"]))
    with factory() as db:
        assert db.scalar(select(func.count(Volunteer.id))) == 0
    assert "cadastrado" in bodies(send(factory, COORD, "SIM"))
    with factory() as db:
        member = db.scalar(select(Volunteer).where(Volunteer.phone_e164 == SECOND))
        assert member is not None
        assert not member.invite_opt_in
        member_id = member.id
    send(factory, COORD, f"EDITAR {member_id}")
    send(factory, COORD, f"FUNCOES {ids['open']},{ids['close']}")
    send(factory, COORD, "SIM")
    with factory() as db:
        roles = set(
            db.scalars(select(VolunteerRole.role_id).where(VolunteerRole.volunteer_id == member_id))
        )
        assert roles == {ids["open"], ids["close"]}


def test_coordinator_creates_service_opens_month_and_blocks_capacity_reduction(roster):
    engine, factory, ids = roster
    member = add(engine, ids)
    second_member = add(engine, ids, SECOND)
    day = next_day()
    send(factory, COORD, "MENU")
    send(factory, COORD, "2")
    send(factory, COORD, day.isoformat())
    send(factory, COORD, "19:00")
    send(factory, COORD, f"{ids['offer']}:2")
    assert "criado" in bodies(send(factory, COORD, "SIM"))
    with factory() as db:
        slot_id = db.scalar(select(Slot.id))
    send(factory, COORD, "MENU")
    send(factory, COORD, "4")
    send(factory, COORD, f"ABRIR {day:%Y-%m}")
    assert "OPEN" in bodies(send(factory, COORD, "SIM"))
    engine.assign(member, slot_id)
    engine.assign(second_member, slot_id)
    with factory.begin() as db:
        with pytest.raises(NoCapacity):
            set_slot_capacity(db, slot_id, 1)
    with factory() as db:
        assert db.get(Slot, slot_id).capacity == 2


def test_volunteer_limit_full_slot_and_cancelled_flow(roster):
    engine, factory, ids = roster
    add(engine, ids)
    add(engine, ids, SECOND)
    day = next_day()
    service_id, slot_id = open_service(roster, day)
    send(factory, FIRST, "Oi")
    send(factory, FIRST, "1")
    send(factory, FIRST, str(service_id))
    send(factory, FIRST, str(slot_id))
    send(factory, FIRST, "CANCELAR")
    with factory() as db:
        assert db.scalar(select(func.count(Assignment.id))) == 0
    send(factory, FIRST, "1")
    send(factory, FIRST, str(service_id))
    send(factory, FIRST, str(slot_id))
    send(factory, FIRST, "SIM")
    send(factory, SECOND, "Oi")
    send(factory, SECOND, "1")
    send(factory, SECOND, str(service_id))
    send(factory, SECOND, str(slot_id))
    assert "vaga" in bodies(send(factory, SECOND, "SIM")).lower()


def test_volunteer_unavailability_and_replacement_manual(roster):
    engine, factory, ids = roster
    first_id = add(engine, ids)
    second_id = add(engine, ids, SECOND)
    _, slot_id = open_service(roster)
    assignment_id = engine.assign(first_id, slot_id)
    send(factory, FIRST, "MENU")
    send(factory, FIRST, "4")
    send(factory, FIRST, str(assignment_id))
    assert "registrado" in bodies(send(factory, FIRST, "SIM"))
    with factory() as db:
        request = db.scalar(select(ReplacementRequest))
        assert request.status == "pending"
        assert db.get(Assignment, assignment_id).status == "replacement_requested"
        request_id = request.id
    send(factory, COORD, "MENU")
    send(factory, COORD, "7")
    send(factory, COORD, f"SUBSTITUIR {request_id} {second_id}")
    assert "confirmada" in bodies(send(factory, COORD, "SIM"))
    with factory() as db:
        assert db.get(Assignment, assignment_id).status == "replaced"
        assert db.get(ReplacementRequest, request_id).status == "fulfilled"
        assert (
            db.scalar(select(func.count(Assignment.id)).where(Assignment.status == "confirmed"))
            == 1
        )


def test_third_day_invitation_requires_acceptance_and_can_be_declined(roster):
    engine, factory, ids = roster
    member = add(engine, ids)
    first_of_next_month = date.today().replace(day=1) + timedelta(days=32)
    first_of_next_month = first_of_next_month.replace(day=1)
    days = [first_of_next_month + timedelta(days=n) for n in (0, 3, 6, 9)]
    slots = [open_service(roster, day)[1] for day in days]
    engine.assign(member, slots[0])
    engine.assign(member, slots[1])
    send(factory, COORD, "MENU")
    send(factory, COORD, "7")
    send(factory, COORD, f"EXCECAO {member} {slots[2]}")
    send(factory, COORD, "Necessidade específica")
    send(factory, COORD, "SIM")
    with factory() as db:
        request_id = db.scalar(select(func.max(ThirdDayRequest.id)))
        assert engine.monthly_days(member, days[2]) == 2
    assert "recusado" in bodies(send(factory, FIRST, f"RECUSAR {request_id}"))
    send(factory, COORD, "MENU")
    send(factory, COORD, "7")
    send(factory, COORD, f"EXCECAO {member} {slots[3]}")
    send(factory, COORD, "Outra necessidade")
    send(factory, COORD, "SIM")
    with factory() as db:
        accepted_id = db.scalar(select(func.max(ThirdDayRequest.id)))
    assert "confirmada" in bodies(send(factory, FIRST, f"ACEITAR {accepted_id}"))
    assert engine.monthly_days(member, days[3]) == 3


def test_schedule_publication_versions_and_invalid_transitions(roster):
    engine, factory, ids = roster
    first_id = add(engine, ids)
    second_id = add(engine, ids, SECOND)
    day = next_day()
    _, slot_id = open_service(roster, day)
    assignment_id = engine.assign(first_id, slot_id)
    with factory.begin() as db:
        version = publish_schedule(db, ids["ministry"], day)
        assert version.version == 1
        with pytest.raises(RosterError):
            transition_schedule(schedule_for(db, ids["ministry"], day), "open")
        with pytest.raises(RosterError):
            transition_assignment(db.get(Assignment, assignment_id), "replaced")
        assert slot_state(db, db.get(Slot, slot_id), schedule_state="published") == "FILLED"
    with factory.begin() as db:
        request_id = request_replacement(db, first_id, assignment_id)
        manual_replace(db, engine, request_id, second_id)
    with factory() as db:
        schedule = schedule_for(db, ids["ministry"], day)
        assert schedule.version == 2
        latest = db.scalar(
            select(ScheduleVersion)
            .where(ScheduleVersion.schedule_id == schedule.id)
            .order_by(ScheduleVersion.version.desc())
        )
        assert "ATUALIZAÇÃO" in latest.update_message
        text = format_schedule(db, ids["ministry"], day)
        assert "Pessoa Exemplo" in text


def test_expired_conversation_and_optout(roster):
    engine, factory, ids = roster
    add(engine, ids)
    send(factory, FIRST, "Oi")
    send(factory, FIRST, "1")
    with factory.begin() as db:
        session = db.scalar(select(ConversationSession))
        session.expires_at = datetime.now(UTC) - timedelta(minutes=1)
    assert "ESCALAS" in bodies(send(factory, FIRST, "1"))
    send(factory, FIRST, "PARAR")
    with factory() as db:
        assert not db.scalar(select(Volunteer.invite_opt_in).where(Volunteer.phone_e164 == FIRST))
    send(factory, FIRST, "INICIAR")
    with factory() as db:
        assert db.scalar(select(Volunteer.invite_opt_in).where(Volunteer.phone_e164 == FIRST))


def test_ordinary_volunteer_cannot_use_admin_commands(roster):
    engine, factory, ids = roster
    add(engine, ids)
    send(factory, FIRST, "OI")
    assert "ESCALAS" in bodies(send(factory, FIRST, "ADMIN"))
    with factory() as db:
        assert db.scalar(select(func.count(Volunteer.id))) == 1


def signed_payload(payload: dict, secret="test-secret"):
    raw = json.dumps(payload).encode()
    signature = "sha256=" + hmac.new(secret.encode(), raw, hashlib.sha256).hexdigest()
    return raw, signature


def meta_envelope(message):
    return {
        "object": "whatsapp_business_account",
        "entry": [
            {
                "changes": [
                    {
                        "field": "messages",
                        "value": {"metadata": {"phone_number_id": "123"}, "messages": [message]},
                    }
                ]
            }
        ],
    }


def test_webhook_verification_signature_and_duplicate(roster):
    _, factory, _ = roster
    config = Settings(
        whatsapp_verify_token="verify-me",
        meta_app_secret="test-secret",
        whatsapp_phone_number_id="123",
    )
    client = TestClient(create_app(factory, config))
    params = {"hub.mode": "subscribe", "hub.verify_token": "verify-me", "hub.challenge": "abc"}
    assert client.get("/webhooks/whatsapp", params=params).text == "abc"
    assert (
        client.get("/webhooks/whatsapp", params={**params, "hub.verify_token": "wrong"}).status_code
        == 403
    )
    raw, sig = signed_payload(
        meta_envelope(
            {"id": "wamid.demo1", "from": "15550100001", "type": "text", "text": {"body": "Oi"}}
        )
    )
    assert (
        client.post(
            "/webhooks/whatsapp", content=raw, headers={"X-Hub-Signature-256": "bad"}
        ).status_code
        == 403
    )
    assert client.post(
        "/webhooks/whatsapp", content=raw, headers={"X-Hub-Signature-256": sig}
    ).json() == {"accepted": 1}
    assert client.post(
        "/webhooks/whatsapp", content=raw, headers={"X-Hub-Signature-256": sig}
    ).json() == {"accepted": 0}
    with factory() as db:
        assert db.scalar(select(func.count(InboundEvent.id))) == 1


def test_interactive_payloads_and_unknown_messages():
    assert (
        parse_message(
            {
                "id": "a",
                "from": "15550100001",
                "type": "interactive",
                "interactive": {"type": "list_reply", "list_reply": {"id": "2"}},
            }
        ).text
        == "2"
    )
    assert (
        parse_message(
            {
                "id": "b",
                "from": "15550100001",
                "type": "interactive",
                "interactive": {"type": "button_reply", "button_reply": {"id": "SIM"}},
            }
        ).text
        == "SIM"
    )
    assert parse_message({"id": "c", "from": "15550100001", "type": "image"}) is None


def test_meta_client_success_and_limits_without_network():
    requests = []

    def ok(request):
        requests.append(request)
        return httpx.Response(200, json={"messages": [{"id": "wamid.sent"}]})

    config = Settings(whatsapp_access_token="test-token", whatsapp_phone_number_id="123")
    client = WhatsAppClient(config, transport=httpx.MockTransport(ok))
    assert client.send(FIRST, text_payload("Oi")) == "wamid.sent"
    assert requests[0].url.path.endswith("/123/messages")
    assert json.loads(requests[0].content)["to"] == FIRST.lstrip("+")
    failing = WhatsAppClient(
        config,
        transport=httpx.MockTransport(
            lambda _: httpx.Response(429, json={"error": {"code": 130429}})
        ),
    )
    with pytest.raises(MetaSendError) as error:
        failing.send(FIRST, text_payload("Oi"))
    assert error.value.code == "130429"


def test_simulator_accepts_list_and_button_replies(roster):
    engine, factory, ids = roster
    add(engine, ids)
    service_id, slot_id = open_service(roster)
    send(factory, FIRST, "Oi")
    simulate(FIRST, "1", reply_kind="list_reply", factory=factory)
    simulate(FIRST, str(service_id), reply_kind="list_reply", factory=factory)
    simulate(FIRST, str(slot_id), reply_kind="list_reply", factory=factory)
    answer = simulate(FIRST, "SIM", reply_kind="button_reply", factory=factory)
    assert "CONFIRMADA" in bodies(answer)


def test_monthly_limit_and_role_conflict_through_conversation(roster):
    engine, factory, ids = roster
    member = add(engine, ids, roles=[ids["open"], ids["offer"]])
    start = (date.today().replace(day=1) + timedelta(days=32)).replace(day=1)
    slots = []
    for offset in (0, 3, 6):
        service_id, role_slots = make_service(
            engine,
            factory,
            ids["ministry"],
            start + timedelta(days=offset),
            {ids["open"]: 1, ids["offer"]: 1},
        )
        slots.append((service_id, role_slots))
    with factory.begin() as db:
        transition_schedule(schedule_for(db, ids["ministry"], start, create=True), "open")
    engine.assign(member, slots[0][1][ids["open"]])
    send(factory, FIRST, "Oi")
    send(factory, FIRST, "1")
    send(factory, FIRST, str(slots[0][0]))
    send(factory, FIRST, str(slots[0][1][ids["offer"]]))
    assert "conflict" in bodies(send(factory, FIRST, "SIM"))
    engine.assign(member, slots[1][1][ids["open"]])
    send(factory, FIRST, "MENU")
    send(factory, FIRST, "1")
    send(factory, FIRST, str(slots[2][0]))
    send(factory, FIRST, str(slots[2][1][ids["open"]]))
    assert "limite mensal" in bodies(send(factory, FIRST, "SIM"))


def test_unavailability_conversation_starts_replacement(roster):
    engine, factory, ids = roster
    member = add(engine, ids)
    day = next_day()
    _, slot_id = open_service(roster, day)
    assignment_id = engine.assign(member, slot_id)
    send(factory, FIRST, "MENU")
    send(factory, FIRST, "3")
    assert "registrada" in bodies(send(factory, FIRST, f"{day} {day}"))
    with factory() as db:
        assert db.get(Assignment, assignment_id).status == "replacement_requested"
        assert db.scalar(select(func.count(ReplacementRequest.id))) == 1


def test_coordinator_is_limited_to_granted_ministry(roster):
    engine, factory, ids = roster
    with factory.begin() as db:
        other = Ministry(church_id=ids["church"], name="Música")
        db.add(other)
        db.flush()
        role = Role(ministry_id=other.id, name="Som")
        db.add(role)
        db.flush()
        other_id, role_id = other.id, role.id
    _, other_slots = make_service(engine, factory, other_id, next_day(), {role_id: 1})
    send(factory, COORD, "MENU")
    send(factory, COORD, "3")
    assert "fora do ministério" in bodies(send(factory, COORD, str(other_slots[role_id])))


def test_general_admin_creates_ministry_with_confirmation(roster):
    _, factory, ids = roster
    phone = "+15550109999"
    with factory.begin() as db:
        db.add(GeneralAdmin(church_id=ids["church"], phone_e164=phone))
    send(factory, phone, "ADMIN")
    send(factory, phone, "MINISTERIO Música")
    with factory() as db:
        assert db.scalar(select(func.count(Ministry.id))) == 1
    send(factory, phone, "SIM")
    with factory() as db:
        assert db.scalar(select(func.count(Ministry.id))) == 2
        new_ministry = db.scalar(select(Ministry).where(Ministry.name == "Música"))
        new_id = new_ministry.id
    send(factory, phone, "ADMIN")
    send(factory, phone, f"FUNCAO {new_id} Som")
    send(factory, phone, "SIM")
    with factory() as db:
        assert db.scalar(select(Role.name).where(Role.ministry_id == new_id)) == "Som"
    send(factory, phone, "ADMIN")
    send(factory, phone, f"COORDENADOR +15550108888 Marcos Exemplo | {new_id}")
    send(factory, phone, "SIM")
    with factory() as db:
        coordinator_id = db.scalar(
            select(Coordinator.id).where(Coordinator.phone_e164 == "+15550108888")
        )
    send(factory, phone, "ADMIN")
    send(factory, phone, f"EDITAR_COORD {coordinator_id} DESATIVAR")
    send(factory, phone, "SIM")
    with factory() as db:
        assert not db.get(Coordinator, coordinator_id).active


def test_webhook_deduplicated_event_runs_business_flow_once(roster):
    engine, factory, ids = roster
    add(engine, ids)
    config = Settings(meta_app_secret="test-secret", whatsapp_phone_number_id="123")
    client = TestClient(create_app(factory, config))
    raw, sig = signed_payload(
        meta_envelope(
            {"id": "wamid.once", "from": FIRST.lstrip("+"), "type": "text", "text": {"body": "Oi"}}
        )
    )
    for _ in range(2):
        assert (
            client.post(
                "/webhooks/whatsapp", content=raw, headers={"X-Hub-Signature-256": sig}
            ).status_code
            == 200
        )
    worker = Worker(session_factory=factory, config=config)
    assert worker.process_one()
    assert not worker.process_one()
    with factory() as db:
        assert db.scalar(select(func.count(OutboundMessage.id))) == 1
        assert db.scalar(select(InboundEvent.status)) == "processed"


def test_delivery_status_and_worker_window_and_retry(roster):
    _, factory, _ = roster

    class FakeClient:
        def __init__(self):
            self.calls = 0

        def send(self, recipient, payload):
            self.calls += 1
            if self.calls <= 2:
                raise MetaSendError(429, "130429")
            return "wamid.ok"

    fake = FakeClient()
    with factory.begin() as db:
        db.add(
            ConversationSession(
                phone_e164=FIRST,
                state="menu",
                data={},
                expires_at=datetime.now(UTC),
                last_inbound_at=datetime.now(UTC),
                rate_window_at=datetime.now(UTC),
            )
        )
        db.add(OutboundMessage(recipient_e164=FIRST, payload=text_payload("Oi")))
    worker = Worker(session_factory=factory, client=fake)
    for _ in range(3):
        assert worker.send_one()
    with factory.begin() as db:
        outgoing = db.scalar(select(OutboundMessage))
        assert outgoing.status == "sent"
        assert outgoing.attempts == 3
        db.add(
            InboundEvent(
                event_id="delivery-1",
                kind="status",
                payload={"id": "wamid.ok", "status": "delivered", "timestamp": "1"},
            )
        )
    worker.process_one()
    with factory() as db:
        assert db.scalar(select(OutboundMessage.status)) == "delivered"


def test_worker_blocks_free_text_outside_window_and_ambiguous_network(roster):
    _, factory, _ = roster

    class TimeoutClient:
        def send(self, recipient, payload):
            raise httpx.ReadTimeout("ambiguous")

    with factory.begin() as db:
        db.add(
            ConversationSession(
                phone_e164=FIRST,
                state="menu",
                data={},
                expires_at=datetime.now(UTC),
                last_inbound_at=datetime.now(UTC) - timedelta(hours=25),
                rate_window_at=datetime.now(UTC),
            )
        )
        db.add(OutboundMessage(recipient_e164=FIRST, payload=text_payload("Oi")))
    worker = Worker(session_factory=factory, client=TimeoutClient())
    worker.send_one()
    with factory.begin() as db:
        outgoing = db.scalar(select(OutboundMessage))
        assert outgoing.status == "blocked_window"
        db.scalar(select(ConversationSession)).last_inbound_at = datetime.now(UTC)
        outgoing.status = "pending"
    worker.send_one()
    with factory() as db:
        assert db.scalar(select(OutboundMessage.status)) == "unknown"


def test_worker_quarantines_poison_event_after_bounded_attempts(roster):
    _, factory, _ = roster
    with factory.begin() as db:
        db.add(
            InboundEvent(
                event_id="poison",
                kind="message",
                payload={
                    "id": "poison",
                    "from": FIRST.lstrip("+"),
                    "type": "text",
                    "text": {"body": "Oi"},
                },
            )
        )
    worker = Worker(session_factory=factory)

    def broken(_db, _message):
        raise RuntimeError("injected failure")

    worker.bot.handle = broken
    for _ in range(3):
        assert worker.process_one()
    with factory() as db:
        event = db.scalar(select(InboundEvent))
        assert event.status == "failed"
        assert event.attempts == 3
        assert event.error_code == "RuntimeError"
        assert db.scalar(select(func.count(OutboundMessage.id))) == 0


def test_delivery_status_does_not_regress_when_events_arrive_out_of_order(roster):
    _, factory, _ = roster
    with factory.begin() as db:
        db.add(
            OutboundMessage(
                recipient_e164=FIRST,
                payload=text_payload("Oi"),
                meta_message_id="wamid.order",
                status="read",
            )
        )
        db.add(
            InboundEvent(
                event_id="old-status",
                kind="status",
                payload={"id": "wamid.order", "status": "sent", "timestamp": "1"},
            )
        )
    Worker(session_factory=factory).process_one()
    with factory() as db:
        assert db.scalar(select(OutboundMessage.status)) == "read"


def test_failed_manual_replacement_keeps_original_and_pending_request(roster):
    engine, factory, ids = roster
    first = add(engine, ids)
    unauthorized = add(engine, ids, SECOND, roles=[ids["open"]])
    _, slot_id = open_service(roster)
    assignment_id = engine.assign(first, slot_id)
    with factory.begin() as db:
        request_id = request_replacement(db, first, assignment_id)
    send(factory, COORD, "MENU")
    send(factory, COORD, "7")
    send(factory, COORD, f"SUBSTITUIR {request_id} {unauthorized}")
    assert "Não foi possível" in bodies(send(factory, COORD, "SIM"))
    with factory() as db:
        assert db.get(Assignment, assignment_id).status == "replacement_requested"
        assert db.get(ReplacementRequest, request_id).status == "pending"
        assert db.scalar(select(func.count(Assignment.id))) == 1


def test_reservation_confirmation_revalidates_permissions_and_rolls_back_failure(roster):
    engine, factory, ids = roster
    member = add(engine, ids, roles=[ids["open"]])
    _, slot_id = open_service(roster)
    with factory.begin() as db:
        reservation = Assignment(
            volunteer_id=member, slot_id=slot_id, status="pending_confirmation"
        )
        db.add(reservation)
        db.flush()
        reservation_id = reservation.id
        with pytest.raises(RosterError):
            transition_assignment(reservation, "confirmed")
    with factory.begin() as db:
        with pytest.raises(RosterError):
            engine.confirm_reservation(reservation_id, db=db)
    with factory() as db:
        assert db.get(Assignment, reservation_id).status == "pending_confirmation"
    with factory.begin() as db:
        db.add(VolunteerRole(volunteer_id=member, role_id=ids["offer"]))
    assert engine.confirm_reservation(reservation_id) == reservation_id
    with factory() as db:
        assert db.get(Assignment, reservation_id).status == "confirmed"


def test_roster_engine_rejects_new_registration_on_closed_schedule(roster):
    engine, factory, ids = roster
    member = add(engine, ids)
    _, slot_id = open_service(roster)
    with factory.begin() as db:
        schedule = db.scalar(select(MonthlySchedule))
        transition_schedule(schedule, "closed")
    with pytest.raises(RosterError):
        engine.assign(member, slot_id)


def test_duplicate_member_registration_returns_safe_error_and_keeps_inbox_processed(roster):
    engine, factory, ids = roster
    add(engine, ids)
    for message in ("MENU", "1", "NOVO", "Outra Pessoa", FIRST, str(ids["offer"])):
        send(factory, COORD, message)
    reply = bodies(send(factory, COORD, "SIM"))
    assert "já existe" in reply
    assert "INSERT" not in reply
    with factory() as db:
        assert db.scalar(select(func.count(Volunteer.id))) == 1
        assert set(db.scalars(select(InboundEvent.status))) == {"processed"}


def test_coordinator_edits_empty_service_and_cannot_move_assigned_service(roster):
    engine, factory, ids = roster
    member = add(engine, ids)
    service_id, slot_id = open_service(roster)
    for message in ("MENU", "2", f"EDITAR {service_id}", "HORA 20:00", "SIM"):
        send(factory, COORD, message)
    with factory() as db:
        assert db.get(WorshipService, service_id).start_time.hour == 20
    engine.assign(member, slot_id)
    for message in ("MENU", "2", f"EDITAR {service_id}", "HORA 21:00"):
        send(factory, COORD, message)
    assert "Não foi possível" in bodies(send(factory, COORD, "SIM"))
    with factory() as db:
        assert db.get(WorshipService, service_id).start_time.hour == 20
