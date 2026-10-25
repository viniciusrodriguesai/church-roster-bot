"""Substitution offers are proactive; only acceptance can release the original."""

from datetime import UTC, date, datetime, time, timedelta

import httpx
import pytest
from sqlalchemy import func, select

from church_roster_bot.core.config import Settings
from church_roster_bot.members.models import Volunteer
from church_roster_bot.ministries.models import Church
from church_roster_bot.notifications.worker import Worker
from church_roster_bot.scheduling.lifecycle import manual_replace, request_replacement
from church_roster_bot.scheduling.models import (
    Assignment,
    AutomationInvitation,
    ReplacementRequest,
    ThirdDayRequest,
)
from church_roster_bot.scheduling.offers import expire_offers, respond_offer
from church_roster_bot.scheduling.replacements import dispatch_replacements
from church_roster_bot.scripts.simulate import simulate
from church_roster_bot.services.models import Slot, WorshipService
from church_roster_bot.whatsapp.client import MetaSendError
from church_roster_bot.whatsapp.models import OutboundMessage
from tests.conftest import make_service
from tests.test_distribution import prepare
from tests.test_invitation_delivery import RecordingClient, drain
from tests.test_phase2 import FIRST, SECOND, bodies

pytestmark = pytest.mark.parametrize(
    "roster", ["sqlite", pytest.param("postgres", marks=pytest.mark.postgres)], indirect=True
)
CONFIG = Settings(
    _env_file=None,
    whatsapp_automation_template="substituicao_ficticia",
    whatsapp_notification_template="aviso_ficticio",
)


def setup_replacement(roster, *, candidates=True):
    engine, factory, _ = roster
    month = (date.today().replace(day=1) + timedelta(days=32)).replace(day=1)
    members, slots = prepare(roster, month=month, members=3)
    original = engine.assign(members[0], slots[0])
    with factory.begin() as db:
        request_id = request_replacement(db, members[0], original)
        if not candidates:
            for member in members[1:]:
                db.get(Volunteer, member).invite_opt_in = False
    return members, slots[0], original, request_id


def dispatch(roster):
    engine, factory, _ = roster
    with factory.begin() as db:
        return dispatch_replacements(db, engine, CONFIG)


def pending_offer(roster):
    with roster[1]() as db:
        return db.scalar(
            select(AutomationInvitation).where(AutomationInvitation.status == "pending")
        )


def answer(roster, member, offer_id, accept=True):
    engine, factory, _ = roster
    with factory.begin() as db:
        return respond_offer(db, engine, member, offer_id, accept, CONFIG)


def test_automatically_invites_and_only_acceptance_replaces_original(roster):
    _, factory, _ = roster
    members, _, original, request_id = setup_replacement(roster)
    assert dispatch(roster) == 1 and dispatch(roster) == 0
    offer = pending_offer(roster)
    assert (offer.kind, offer.volunteer_id, offer.replacement_request_id) == (
        "replacement",
        members[1],
        request_id,
    )
    client = drain(factory, CONFIG)
    assert any(phone == SECOND for phone, _ in client.calls)
    with factory() as db:
        assert db.get(Assignment, original).status == "replacement_requested"
        assert db.scalar(select(func.count(Assignment.id))) == 1
    first = answer(roster, members[1], offer.id)
    assert first.status == "accepted" and answer(roster, members[1], offer.id) == first
    drain(factory, CONFIG, client)
    with factory() as db:
        request = db.get(ReplacementRequest, request_id)
        assert (request.status, request.replacement_assignment_id) == (
            "fulfilled",
            first.assignment_id,
        )
        assert db.get(Assignment, original).status == "replaced"
        assert db.get(Assignment, first.assignment_id).status == "confirmed"
        assert db.scalar(select(func.count(Assignment.id))) == 2
        assert (
            db.scalar(
                select(func.count(OutboundMessage.id)).where(
                    OutboundMessage.volunteer_id == members[0],
                    OutboundMessage.dedupe_key.like("%:fulfilled"),
                )
            )
            == 1
        )
    assert sum(phone == FIRST for phone, _ in client.calls) == 1


def test_decline_and_expiry_try_another_candidate_without_reinviting_the_first(roster):
    _, factory, _ = roster
    members, _, original, _ = setup_replacement(roster)
    dispatch(roster)
    first = pending_offer(roster)
    assert answer(roster, members[1], first.id, False).status == "declined"
    assert dispatch(roster) == 1
    second = pending_offer(roster)
    assert second.volunteer_id == members[2]
    with factory.begin() as db:
        offer = db.get(AutomationInvitation, second.id)
        offer.created_at = datetime.now(UTC) - timedelta(hours=2)
        offer.expires_at = datetime.now(UTC) - timedelta(hours=1)
    with factory.begin() as db:
        expire_offers(db, CONFIG)
    assert dispatch(roster) == 0
    with factory() as db:
        assert db.scalar(select(func.count(AutomationInvitation.id))) == 2
        assert db.get(Assignment, original).status == "replacement_requested"


def test_no_candidate_alert_is_deduplicated_and_a_later_opt_in_can_recover(roster):
    _, factory, _ = roster
    members, _, original, request_id = setup_replacement(roster, candidates=False)
    assert dispatch(roster) == dispatch(roster) == 0
    with factory() as db:
        assert (
            db.scalar(
                select(func.count(OutboundMessage.id)).where(
                    OutboundMessage.dedupe_key.like(f"replacement:{request_id}:no_candidate:%")
                )
            )
            == 1
        )
        assert db.get(Assignment, original).status == "replacement_requested"
    simulate(SECOND, "INICIAR", factory=factory, config=CONFIG)
    assert dispatch(roster) == 1 and pending_offer(roster).volunteer_id == members[1]


def test_current_monthly_limit_is_never_bypassed_for_a_substitute(roster):
    engine, factory, ids = roster
    members, slot_id, _, _ = setup_replacement(roster)
    with factory() as db:
        day = db.get(WorshipService, db.get(Slot, slot_id).service_id).service_date
    for offset in (3, 6):
        _, slots = make_service(
            engine, factory, ids["ministry"], day + timedelta(days=offset), {ids["offer"]: 1}
        )
        engine.assign(members[1], slots[ids["offer"]])
    assert dispatch(roster) == 1 and pending_offer(roster).volunteer_id == members[2]
    with factory() as db:
        assert db.scalar(select(func.count(ThirdDayRequest.id))) == 0


def test_a_candidate_becoming_unavailable_is_cancelled_and_next_is_invited(roster):
    engine, factory, _ = roster
    members, slot_id, _, _ = setup_replacement(roster)
    dispatch(roster)
    first = pending_offer(roster)
    with factory() as db:
        day = db.get(WorshipService, db.get(Slot, slot_id).service_id).service_date
    engine.set_unavailability(members[1], day, day)
    assert dispatch(roster) == 1
    assert answer(roster, members[1], first.id).status == "cancelled"
    assert pending_offer(roster).volunteer_id == members[2]


def test_manual_fulfilment_invalidates_a_previously_sent_automatic_offer(roster):
    engine, factory, _ = roster
    members, _, original, request_id = setup_replacement(roster)
    dispatch(roster)
    offer = pending_offer(roster)
    with factory.begin() as db:
        manual_replace(db, engine, request_id, members[2])
    assert dispatch(roster) == 0
    assert answer(roster, members[1], offer.id).status == "cancelled"
    with factory() as db:
        assert db.get(Assignment, original).status == "replaced"
        assert db.scalar(select(func.count(Assignment.id))) == 2


def test_original_notice_obeys_consent_and_iniciar_releases_it_once(roster):
    _, factory, _ = roster
    members, _, _, _ = setup_replacement(roster)
    dispatch(roster)
    offer = pending_offer(roster)
    with factory.begin() as db:
        db.get(Volunteer, members[0]).invite_opt_in = False
    answer(roster, members[1], offer.id)
    with factory() as db:
        outgoing = db.scalar(
            select(OutboundMessage).where(OutboundMessage.volunteer_id == members[0])
        )
        assert (outgoing.status, outgoing.attempts) == ("blocked_consent", 0)
    client = drain(factory, CONFIG)
    assert not any(phone == FIRST for phone, _ in client.calls)
    simulate(FIRST, "INICIAR", factory=factory, config=CONFIG)
    drain(factory, CONFIG, client)
    with factory() as db:
        outgoing = db.scalar(
            select(OutboundMessage).where(OutboundMessage.volunteer_id == members[0])
        )
        assert (outgoing.status, outgoing.attempts) == ("sent", 1)


def test_original_opt_out_before_notice_send_is_rechecked(roster):
    _, factory, _ = roster
    members, _, _, _ = setup_replacement(roster)
    dispatch(roster)
    offer = pending_offer(roster)
    answer(roster, members[1], offer.id)
    with factory.begin() as db:
        db.get(Volunteer, members[0]).invite_opt_in = False
    client = drain(factory, CONFIG)
    assert not any(phone == FIRST for phone, _ in client.calls)


def test_changing_the_original_phone_never_sends_a_notice_to_the_old_number(roster):
    _, factory, _ = roster
    members, _, _, _ = setup_replacement(roster)
    dispatch(roster)
    offer = pending_offer(roster)
    answer(roster, members[1], offer.id)
    with factory.begin() as db:
        db.get(Volunteer, members[0]).phone_e164 = "+15550100099"
    client = drain(factory, CONFIG)
    assert not any(phone == FIRST for phone, _ in client.calls)


def test_indisponibility_chat_starts_automatic_search_without_candidate_commands(roster):
    engine, factory, _ = roster
    month = (date.today().replace(day=1) + timedelta(days=32)).replace(day=1)
    members, slots = prepare(roster, month=month, members=3)
    engine.assign(members[0], slots[0])
    for text in ("Oi", "3", f"{month.isoformat()} {month.isoformat()}"):
        replies = simulate(FIRST, text, factory=factory, config=CONFIG)
    assert "Indisponibilidade registrada" in bodies(replies)
    client = RecordingClient()
    assert Worker(factory, CONFIG, client).maintain()
    drain(factory, CONFIG, client)
    assert any(phone == SECOND for phone, _ in client.calls)


def test_request_replacement_uses_service_timezone_at_the_utc_date_boundary(roster):
    engine, factory, ids = roster
    members, _ = prepare(roster, month=date(2027, 1, 1), members=2)
    service_id = engine.create_service(
        ids["ministry"], date(2027, 1, 1), time(23), {ids["offer"]: 1}
    )
    with factory() as db:
        slot_id = db.scalar(select(Slot.id).where(Slot.service_id == service_id))
    assignment = engine.assign(members[0], slot_id)
    with factory.begin() as db:
        request_id = request_replacement(
            db, members[0], assignment, now=datetime(2027, 1, 2, 1, tzinfo=UTC)
        )
        assert request_id


def test_invalid_local_service_time_alerts_once_without_stopping_search_worker(roster):
    _, factory, ids = roster
    _, slot_id, original, request_id = setup_replacement(roster)
    with factory.begin() as db:
        db.get(Church, ids["church"]).timezone = "America/New_York"
        service = db.get(WorshipService, db.get(Slot, slot_id).service_id)
        service.service_date, service.start_time = date(2027, 3, 14), time(2, 30)
    assert dispatch(roster) == dispatch(roster) == 0
    with factory() as db:
        assert db.get(Assignment, original).status == "replacement_requested"
        assert (
            db.scalar(
                select(func.count(OutboundMessage.id)).where(
                    OutboundMessage.dedupe_key.like(f"replacement:{request_id}:invalid_time:%")
                )
            )
            == 1
        )


@pytest.mark.parametrize(
    "failure,status",
    [
        (MetaSendError(400, "synthetic"), "failed"),
        (httpx.ReadTimeout("synthetic"), "unknown"),
    ],
)
def test_original_notice_send_failure_never_repeats_or_undoes_replacement(roster, failure, status):
    _, factory, _ = roster
    members, _, original, request_id = setup_replacement(roster)
    dispatch(roster)
    offer = pending_offer(roster)
    answer(roster, members[1], offer.id)
    client = drain(factory, CONFIG, RecordingClient(failure))
    Worker(factory, CONFIG, client).recover_sending()
    drain(factory, CONFIG, client)
    assert sum(phone == FIRST for phone, _ in client.calls) == 1
    with factory() as db:
        notice = db.scalar(
            select(OutboundMessage).where(OutboundMessage.volunteer_id == members[0])
        )
        assert (notice.status, notice.attempts) == (status, 1)
        assert db.get(Assignment, original).status == "replaced"
        assert db.get(ReplacementRequest, request_id).status == "fulfilled"


def test_interrupted_original_notice_is_preserved_as_unknown_without_resend(roster):
    _, factory, _ = roster
    members, _, _, _ = setup_replacement(roster)
    dispatch(roster)
    offer = pending_offer(roster)
    answer(roster, members[1], offer.id)
    with factory.begin() as db:
        notice = db.scalar(
            select(OutboundMessage).where(OutboundMessage.volunteer_id == members[0])
        )
        notice.status, notice.attempts = "sending", 1
    client = RecordingClient()
    Worker(factory, CONFIG, client).recover_sending()
    drain(factory, CONFIG, client)
    assert not any(phone == FIRST for phone, _ in client.calls)
    with factory() as db:
        notice = db.scalar(
            select(OutboundMessage).where(OutboundMessage.volunteer_id == members[0])
        )
        assert (notice.status, notice.error_code) == ("unknown", "worker_interrupted")
