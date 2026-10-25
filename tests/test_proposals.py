"""Persisted drafts preserve their evidence and enforce current ministry access."""

from datetime import date, timedelta

import pytest
from sqlalchemy import func, select

from church_roster_bot.audit.models import AuditEvent
from church_roster_bot.members.models import CoordinatorMinistry
from church_roster_bot.ministries.models import Ministry
from church_roster_bot.scheduling.errors import NotAuthorized, RosterError
from church_roster_bot.scheduling.models import Assignment, RosterProposal, RosterProposalItem
from church_roster_bot.scheduling.proposals import (
    cancel_proposal,
    create_proposal,
    proposal_snapshot,
    require_proposal,
)
from church_roster_bot.scripts.simulate import simulate
from church_roster_bot.whatsapp.models import OutboundMessage
from church_roster_bot.whatsapp.payloads import text_payload
from tests.test_distribution import MONTH, NOW, prepare, propose
from tests.test_phase2 import COORD, bodies

pytestmark = pytest.mark.parametrize(
    "roster", ["sqlite", pytest.param("postgres", marks=pytest.mark.postgres)], indirect=True
)


def create(roster):
    engine, factory, ids = roster
    with factory.begin() as db:
        return create_proposal(
            db,
            engine,
            ids["ministry"],
            MONTH,
            actor_type="coordinator",
            actor_id=ids["coordinator"],
            now=NOW,
        )


def test_draft_round_trips_scores_and_projection_without_assigning(roster):
    _, factory, ids = roster
    prepare(roster, days=(1, 4, 7), members=1)
    expected = propose(roster)
    proposal_id = create(roster)
    # A new session represents a worker/process restart.
    with factory() as db:
        proposal = require_proposal(
            db, proposal_id, ids["ministry"], "coordinator", ids["coordinator"]
        )
        assert proposal.status == "draft"
        assert proposal_snapshot(db, proposal) == expected
        assert db.scalar(select(func.count(Assignment.id))) == 0
        assert db.scalar(select(func.count(OutboundMessage.id))) == 0
        assert db.scalar(select(func.count(RosterProposalItem.id))) == 2
        assert db.scalar(select(AuditEvent.action)) == "proposal_created"


def test_revoked_access_cannot_read_or_cancel_old_drafts(roster):
    _, factory, ids = roster
    prepare(roster)
    proposal_id = create(roster)
    with factory.begin() as db:
        db.delete(db.scalar(select(CoordinatorMinistry)))
    with factory.begin() as db:
        with pytest.raises(NotAuthorized):
            require_proposal(db, proposal_id, ids["ministry"], "coordinator", ids["coordinator"])
        with pytest.raises(NotAuthorized):
            cancel_proposal(db, proposal_id, ids["ministry"], "coordinator", ids["coordinator"])


def test_selected_ministry_cannot_be_used_to_access_another_ministry_draft(roster):
    _, factory, ids = roster
    prepare(roster)
    proposal_id = create(roster)
    with factory.begin() as db:
        other = Ministry(church_id=ids["church"], name="Outro ministério fictício")
        db.add(other)
        db.flush()
        db.add(CoordinatorMinistry(coordinator_id=ids["coordinator"], ministry_id=other.id))
        other_id = other.id
    with factory() as db, pytest.raises(NotAuthorized, match="outside the selected ministry"):
        require_proposal(db, proposal_id, other_id, "coordinator", ids["coordinator"])


def test_cancel_is_idempotent_and_keeps_items_and_audit(roster):
    _, factory, ids = roster
    prepare(roster)
    proposal_id = create(roster)
    for _ in range(2):
        with factory.begin() as db:
            cancel_proposal(db, proposal_id, ids["ministry"], "coordinator", ids["coordinator"])
    with factory() as db:
        assert db.get(RosterProposal, proposal_id).status == "cancelled"
        assert db.scalar(select(func.count(RosterProposalItem.id))) == 1
        assert (
            db.scalar(
                select(func.count(AuditEvent.id)).where(AuditEvent.action == "proposal_cancelled")
            )
            == 1
        )


def test_only_drafts_can_be_cancelled(roster):
    _, factory, ids = roster
    prepare(roster)
    proposal_id = create(roster)
    with factory.begin() as db:
        db.get(RosterProposal, proposal_id).status = "approved"
    with factory.begin() as db, pytest.raises(RosterError, match="only draft"):
        cancel_proposal(db, proposal_id, ids["ministry"], "coordinator", ids["coordinator"])


def test_creation_rolls_back_items_when_audit_fails_even_if_caller_commits(roster, monkeypatch):
    engine, factory, ids = roster
    prepare(roster)

    def fail(*_args, **_kwargs):
        raise RuntimeError("synthetic audit failure")

    monkeypatch.setattr("church_roster_bot.scheduling.proposals.audit", fail)
    with factory.begin() as db:
        with pytest.raises(RuntimeError, match="synthetic"):
            create_proposal(
                db,
                engine,
                ids["ministry"],
                MONTH,
                actor_type="coordinator",
                actor_id=ids["coordinator"],
                now=NOW,
            )
        db.add(OutboundMessage(recipient_e164=COORD, payload=text_payload("Resposta de falha")))
    with factory() as db:
        assert db.scalar(select(func.count(RosterProposal.id))) == 0
        assert db.scalar(select(func.count(RosterProposalItem.id))) == 0
        assert db.scalar(select(func.count(OutboundMessage.id))) == 1


def test_saved_scores_are_not_recalculated_when_assignments_change(roster):
    engine, factory, _ = roster
    members, slots = prepare(roster)
    proposal_id = create(roster)
    with factory() as db:
        original = proposal_snapshot(db, db.get(RosterProposal, proposal_id))
    engine.assign(members[1], slots[0])
    with factory() as db:
        assert proposal_snapshot(db, db.get(RosterProposal, proposal_id)) == original


def test_chat_can_save_review_and_cancel_a_draft_after_restart(roster):
    _, factory, _ = roster
    month = (date.today().replace(day=1) + timedelta(days=32)).replace(day=1)
    prepare(roster, month=month)
    for text in ("Oi", f"PLANEJAR {month:%Y-%m}"):
        replies = simulate(COORD, text, factory=factory)
    assert "Proposta #1 salva como rascunho" in bodies(replies)
    assert "carga projetada" in bodies(simulate(COORD, "VERPROPOSTA 1", factory=factory))
    assert "Descartar proposta #1?" in bodies(simulate(COORD, "DESCARTAR 1", factory=factory))
    assert "histórico preservado" in bodies(simulate(COORD, "SIM", factory=factory))
