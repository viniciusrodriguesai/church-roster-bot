"""Persisted, scoped proposal drafts; generating a draft never assigns volunteers."""

from datetime import UTC, date, datetime
from typing import Literal

from sqlalchemy import select
from sqlalchemy.orm import Session

from church_roster_bot.administration.service import authorize_actor
from church_roster_bot.scheduling.distribution import (
    CandidateScore,
    DistributionPlanner,
    DistributionProposal,
    SuggestedAssignment,
    UnfilledSlot,
)
from church_roster_bot.scheduling.engine import AssignmentIntent, RosterEngine
from church_roster_bot.scheduling.errors import NotAuthorized, NotFound, RosterError
from church_roster_bot.scheduling.lifecycle import audit
from church_roster_bot.scheduling.models import RosterProposal, RosterProposalItem


def create_proposal(
    db: Session,
    roster: RosterEngine,
    ministry_id: int,
    month: date,
    *,
    actor_type: Literal["admin", "coordinator"],
    actor_id: int,
    now: datetime | None = None,
) -> int:
    with db.begin_nested():
        result = DistributionPlanner(roster).propose(
            db, ministry_id, month, actor_type=actor_type, actor_id=actor_id, now=now
        )
        proposal = RosterProposal(
            ministry_id=ministry_id,
            month=result.month,
            policy_version=result.policy_version,
            created_by_type=actor_type,
            created_by_id=actor_id,
            created_at=result.generated_at,
            unfilled=[
                {"slot_id": v.slot_id, "positions": v.positions, "rejections": list(v.rejections)}
                for v in result.unfilled
            ],
        )
        db.add(proposal)
        db.flush()
        for position, suggestion in enumerate(result.suggestions):
            score = suggestion.score
            db.add(
                RosterProposalItem(
                    proposal_id=proposal.id,
                    position=position,
                    slot_id=suggestion.intent.slot_id,
                    volunteer_id=suggestion.intent.volunteer_id,
                    score={
                        "monthly_days": score.monthly_days,
                        "monthly_assignments": score.monthly_assignments,
                        "historical_days": score.historical_days,
                        "last_served": score.last_served.isoformat() if score.last_served else None,
                    },
                )
            )
        ministry = authorize_actor(db, ministry_id, actor_type, actor_id)
        audit(
            db,
            ministry.church_id,
            actor_type,
            actor_id,
            "proposal_created",
            "roster_proposal",
            proposal.id,
            {"items": len(result.suggestions), "policy_version": result.policy_version},
        )
        db.flush()
        return proposal.id


def require_proposal(
    db: Session,
    proposal_id: int,
    ministry_id: int,
    actor_type: str,
    actor_id: int,
    *,
    lock: bool = False,
) -> RosterProposal:
    authorize_actor(db, ministry_id, actor_type, actor_id)
    query = select(RosterProposal).where(RosterProposal.id == proposal_id)
    if lock:
        query = query.with_for_update().execution_options(populate_existing=True)
    proposal = db.scalar(query)
    if proposal is None:
        raise NotFound("proposal not found")
    if proposal.ministry_id != ministry_id:
        raise NotAuthorized("proposal is outside the selected ministry")
    return proposal


def proposal_snapshot(db: Session, proposal: RosterProposal) -> DistributionProposal:
    items = db.scalars(
        select(RosterProposalItem)
        .where(RosterProposalItem.proposal_id == proposal.id)
        .order_by(RosterProposalItem.position)
    ).all()
    generated = proposal.created_at
    if generated.tzinfo is None:
        generated = generated.replace(tzinfo=UTC)
    return DistributionProposal(
        proposal.ministry_id,
        proposal.month,
        generated,
        proposal.policy_version,
        tuple(
            SuggestedAssignment(
                AssignmentIntent(item.volunteer_id, item.slot_id),
                CandidateScore(
                    item.score["monthly_days"],
                    item.score["monthly_assignments"],
                    item.score["historical_days"],
                    date.fromisoformat(item.score["last_served"])
                    if item.score["last_served"]
                    else None,
                ),
            )
            for item in items
        ),
        tuple(
            UnfilledSlot(v["slot_id"], v["positions"], tuple(tuple(r) for r in v["rejections"]))
            for v in proposal.unfilled
        ),
    )


def cancel_proposal(
    db: Session,
    proposal_id: int,
    ministry_id: int,
    actor_type: str,
    actor_id: int,
) -> None:
    with db.begin_nested():
        proposal = require_proposal(db, proposal_id, ministry_id, actor_type, actor_id, lock=True)
        if proposal.status == "cancelled":
            return
        if proposal.status != "draft":
            raise RosterError("only draft proposals can be cancelled")
        proposal.status = "cancelled"
        proposal.decided_at = datetime.now(UTC)
        ministry = authorize_actor(db, ministry_id, actor_type, actor_id)
        audit(
            db,
            ministry.church_id,
            actor_type,
            actor_id,
            "proposal_cancelled",
            "roster_proposal",
            proposal.id,
        )
