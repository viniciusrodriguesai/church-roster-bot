"""Approved proposals use the real outbox; transport success never confirms a roster."""

from datetime import UTC, date, datetime, time, timedelta

import httpx
import pytest
from sqlalchemy import func, select

from church_roster_bot.conversations.models import ConversationSession
from church_roster_bot.core.config import Settings
from church_roster_bot.members.models import CoordinatorMinistry, Volunteer
from church_roster_bot.ministries.models import Church
from church_roster_bot.notifications.worker import Worker
from church_roster_bot.scheduling.errors import NotAuthorized, RosterError
from church_roster_bot.scheduling.lifecycle import schedule_for
from church_roster_bot.scheduling.models import Assignment, AutomationInvitation, RosterProposal
from church_roster_bot.scheduling.offers import approve_proposal, expire_offers, respond_offer
from church_roster_bot.scheduling.proposals import create_proposal
from church_roster_bot.scripts.simulate import simulate
from church_roster_bot.services.models import Slot, WorshipService
from church_roster_bot.whatsapp.client import MetaSendError
from church_roster_bot.whatsapp.models import OutboundMessage
from tests.test_distribution import prepare
from tests.test_invitation_delivery import RecordingClient, drain
from tests.test_phase2 import COORD, FIRST, bodies

pytestmark = pytest.mark.parametrize(
    "roster", ["sqlite", pytest.param("postgres", marks=pytest.mark.postgres)], indirect=True
)


def setup_offer(roster, *, days=(1,), members=2, config=None):
    engine, factory, ids = roster
    month = (date.today().replace(day=1) + timedelta(days=32)).replace(day=1)
    volunteers, slots = prepare(roster, month=month, days=days, members=members)
    config = config or Settings(
        _env_file=None,
        whatsapp_automation_template="convite_ficticio",
        whatsapp_notification_template="aviso_ficticio",
    )
    with factory.begin() as db:
        proposal_id = create_proposal(
            db,
            engine,
            ids["ministry"],
            month,
            actor_type="coordinator",
            actor_id=ids["coordinator"],
        )
    return volunteers, slots, proposal_id, config


def approve(roster, proposal_id, config):
    engine, factory, ids = roster
    with factory.begin() as db:
        return approve_proposal(
            db, engine, proposal_id, ids["ministry"], "coordinator", ids["coordinator"], config
        )


def decision(roster, volunteer_id, offer_id, config, accept=True, **kwargs):
    engine, factory, _ = roster
    with factory.begin() as db:
        return respond_offer(db, engine, volunteer_id, offer_id, accept, config, **kwargs)


def test_approval_sends_once_and_only_acceptance_assigns(roster):
    _, factory, _ = roster
    members, _, proposal_id, config = setup_offer(roster)
    offers = approve(roster, proposal_id, config)
    assert approve(roster, proposal_id, config) == offers
    client = drain(factory, config)
    assert len(client.calls) == 1
    with factory() as db:
        assert db.get(RosterProposal, proposal_id).status == "approved"
        assert db.scalar(select(func.count(Assignment.id))) == 0
        out = db.scalar(
            select(OutboundMessage).where(OutboundMessage.automation_invitation_id == offers[0])
        )
        assert (out.status, out.attempts) == ("sent", 1)
    first = decision(roster, members[0], offers[0], config)
    second = decision(roster, members[0], offers[0], config)
    assert first == second and first.status == "accepted"
    drain(factory, config, client)
    with factory() as db:
        assert db.scalar(select(func.count(Assignment.id))) == 1
        notices = db.scalars(
            select(OutboundMessage).where(
                OutboundMessage.dedupe_key.like(f"automation:{offers[0]}:accepted:%")
            )
        ).all()
        assert len(notices) == 1 and notices[0].attempts == 1


def test_another_volunteer_cannot_answer_the_offer(roster):
    members, _, proposal_id, config = setup_offer(roster)
    offer_id = approve(roster, proposal_id, config)[0]
    with pytest.raises(NotAuthorized):
        decision(roster, members[1], offer_id, config)


def test_rejection_does_not_assign_and_notifies_once(roster):
    _, factory, _ = roster
    members, _, proposal_id, config = setup_offer(roster)
    offer_id = approve(roster, proposal_id, config)[0]
    for _ in range(2):
        assert decision(roster, members[0], offer_id, config, accept=False).status == "declined"
    with factory() as db:
        assert db.scalar(select(func.count(Assignment.id))) == 0
        assert (
            db.scalar(
                select(func.count(OutboundMessage.id)).where(
                    OutboundMessage.dedupe_key.like(f"automation:{offer_id}:declined:%")
                )
            )
            == 1
        )


def test_a_stale_proposal_rolls_back_every_offer_and_outbound(roster):
    engine, factory, _ = roster
    members, slots, proposal_id, config = setup_offer(roster, days=(1, 4))
    engine.assign(members[0], slots[1])
    with pytest.raises(RosterError):
        approve(roster, proposal_id, config)
    with factory() as db:
        assert db.get(RosterProposal, proposal_id).status == "draft"
        assert db.scalar(select(func.count(AutomationInvitation.id))) == 0
        assert db.scalar(select(func.count(OutboundMessage.id))) == 0


def test_a_second_proposal_cannot_duplicate_pending_invitations(roster):
    engine, factory, ids = roster
    _, _, proposal_id, config = setup_offer(roster)
    approve(roster, proposal_id, config)
    with factory.begin() as db:
        second = create_proposal(
            db,
            engine,
            ids["ministry"],
            db.get(RosterProposal, proposal_id).month,
            actor_type="coordinator",
            actor_id=ids["coordinator"],
        )
    with pytest.raises(RosterError, match="pending invitation"):
        approve(roster, second, config)


@pytest.mark.parametrize(
    "change", ["capacity", "unavailability", "monthly_limit", "closed", "service_moved", "timezone"]
)
def test_acceptance_revalidates_current_rules_and_the_promised_service(roster, change):
    engine, factory, ids = roster
    members, slots, proposal_id, config = setup_offer(roster)
    offer_id = approve(roster, proposal_id, config)[0]
    with factory() as db:
        day = db.get(WorshipService, db.get(Slot, slots[0]).service_id).service_date
    if change == "capacity":
        engine.assign(members[1], slots[0])
    elif change == "unavailability":
        engine.set_unavailability(members[0], day, day)
    elif change == "monthly_limit":
        from tests.conftest import make_service

        for offset in (3, 6):
            _, other = make_service(
                engine, factory, ids["ministry"], day + timedelta(days=offset), {ids["offer"]: 1}
            )
            engine.assign(members[0], other[ids["offer"]])
    else:
        with factory.begin() as db:
            if change == "closed":
                schedule_for(db, ids["ministry"], day).state = "closed"
            elif change == "timezone":
                db.get(Church, ids["church"]).timezone = "UTC"
            else:
                db.get(WorshipService, db.get(Slot, slots[0]).service_id).start_time = time(20)
    result = decision(roster, members[0], offer_id, config)
    assert result.status == "cancelled" and result.assignment_id is None


def test_acceptance_can_update_a_published_schedule(roster):
    _, factory, ids = roster
    members, _, proposal_id, config = setup_offer(roster)
    offer_id = approve(roster, proposal_id, config)[0]
    with factory.begin() as db:
        schedule_for(
            db, ids["ministry"], db.get(RosterProposal, proposal_id).month
        ).state = "published"
    assert decision(roster, members[0], offer_id, config).status == "accepted"


def test_revoked_coordinator_cannot_approve_old_proposals(roster):
    _, factory, _ = roster
    _, _, proposal_id, config = setup_offer(roster)
    with factory.begin() as db:
        db.delete(db.scalar(select(CoordinatorMinistry)))
    with pytest.raises(NotAuthorized):
        approve(roster, proposal_id, config)


def test_consent_blocks_delivery_and_iniciar_releases_only_unsent_invites(roster):
    _, factory, _ = roster
    members, _, proposal_id, config = setup_offer(roster)
    with factory.begin() as db:
        db.get(Volunteer, members[0]).invite_opt_in = False
    offer_id = approve(roster, proposal_id, config)[0]
    with factory() as db:
        out = db.scalar(
            select(OutboundMessage).where(OutboundMessage.automation_invitation_id == offer_id)
        )
        assert (out.status, out.attempts) == ("blocked_consent", 0)
    simulate(FIRST, "INICIAR", factory=factory, config=config)
    client = drain(factory, config)
    offer_calls = [
        payload
        for phone, payload in client.calls
        if phone == FIRST and payload["type"] == "interactive"
    ]
    assert len(offer_calls) == 1
    simulate(FIRST, "INICIAR", factory=factory, config=config)
    before = len(client.calls)
    drain(factory, config, client)
    assert len(client.calls) == before + 1  # Only the explicit INICIAR acknowledgement.


def test_outside_window_requires_the_approved_automation_template(roster):
    _, factory, _ = roster
    _, _, proposal_id, config = setup_offer(roster, config=Settings(_env_file=None))
    offer_id = approve(roster, proposal_id, config)[0]
    with factory() as db:
        out = db.scalar(
            select(OutboundMessage).where(OutboundMessage.automation_invitation_id == offer_id)
        )
        assert out.status == "blocked_window" and out.attempts == 0


def test_templates_contain_context_and_unambiguous_commands(roster):
    _, factory, _ = roster
    _, _, proposal_id, config = setup_offer(roster)
    offer_id = approve(roster, proposal_id, config)[0]
    with factory() as db:
        out = db.scalar(
            select(OutboundMessage).where(OutboundMessage.automation_invitation_id == offer_id)
        )
        assert out.payload["type"] == "template"
        body = out.payload["template"]["components"][0]["parameters"][0]["text"]
        assert f"ACEITAR A{offer_id}" in body and f"RECUSAR A{offer_id}" in body
        assert "carga projetada" in body and "19:00" in body


@pytest.mark.parametrize(
    "error,status",
    [
        (MetaSendError(400, "synthetic"), "failed"),
        (httpx.ReadTimeout("synthetic"), "unknown"),
    ],
)
def test_transport_failures_are_not_resent_and_never_confirm(roster, error, status):
    _, factory, _ = roster
    _, _, proposal_id, config = setup_offer(roster)
    offer_id = approve(roster, proposal_id, config)[0]
    client = drain(factory, config, RecordingClient(error))
    before = len(client.calls)
    drain(factory, config, client)
    assert len(client.calls) == before
    with factory() as db:
        out = db.scalar(
            select(OutboundMessage).where(OutboundMessage.automation_invitation_id == offer_id)
        )
        assert out.status == status and out.attempts == 1
        assert db.scalar(select(func.count(Assignment.id))) == 0


def test_interrupted_sending_is_recovered_without_resending(roster):
    _, factory, _ = roster
    _, _, proposal_id, config = setup_offer(roster)
    offer_id = approve(roster, proposal_id, config)[0]
    with factory.begin() as db:
        outgoing = db.scalar(
            select(OutboundMessage).where(OutboundMessage.automation_invitation_id == offer_id)
        )
        outgoing.status, outgoing.attempts = "sending", 1
    client = RecordingClient()
    Worker(factory, config, client).recover_sending()
    drain(factory, config, client)
    assert not any(phone == FIRST for phone, _ in client.calls)


def test_worker_expiry_is_idempotent_and_rejects_late_acceptance(roster):
    _, factory, _ = roster
    members, _, proposal_id, config = setup_offer(roster)
    offer_id = approve(roster, proposal_id, config)[0]
    with factory() as db:
        deadline = db.get(AutomationInvitation, offer_id).expires_at
        if deadline.tzinfo is None:
            deadline = deadline.replace(tzinfo=UTC)
    for expected in (1, 0):
        with factory.begin() as db:
            assert expire_offers(db, config, now=deadline + timedelta(seconds=1)) == expected
    assert decision(roster, members[0], offer_id, config).status == "expired"


def test_chat_approval_confirmation_and_acceptance_work_after_session_expiry(roster):
    _, factory, _ = roster
    _, _, proposal_id, config = setup_offer(roster)
    simulate(COORD, "Oi", factory=factory, config=config)
    simulate(COORD, f"APROVAR {proposal_id}", factory=factory, config=config)
    assert "Proposta aprovada" in bodies(simulate(COORD, "SIM", factory=factory, config=config))
    simulate(FIRST, "Oi", factory=factory, config=config)
    with factory.begin() as db:
        db.scalar(
            select(ConversationSession).where(ConversationSession.phone_e164 == FIRST)
        ).expires_at = datetime.now(UTC) - timedelta(days=1)
    replies = simulate(FIRST, "ACEITAR A1", factory=factory, config=config)
    assert "inscrição confirmada" in bodies(replies)
