"""Approve proposals as invitations; only an explicit, eligible acceptance assigns."""

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from church_roster_bot.administration.service import authorize_actor
from church_roster_bot.core.config import Settings
from church_roster_bot.core.time import database_utc, utc_now
from church_roster_bot.members.models import Volunteer
from church_roster_bot.ministries.models import Ministry
from church_roster_bot.notifications.automation import (
    notify_offer,
    service_fingerprint,
    service_start,
)
from church_roster_bot.scheduling.engine import AssignmentIntent, RosterEngine
from church_roster_bot.scheduling.errors import NotAuthorized, RosterError
from church_roster_bot.scheduling.invitations import create_invitation
from church_roster_bot.scheduling.lifecycle import audit, publish_schedule, schedule_for
from church_roster_bot.scheduling.models import (
    AutomationInvitation,
    RosterProposalItem,
    WaitlistEntry,
)
from church_roster_bot.scheduling.proposals import proposal_snapshot, require_proposal
from church_roster_bot.services.models import Slot, WorshipService


def approve_proposal(
    db: Session,
    roster: RosterEngine,
    proposal_id: int,
    ministry_id: int,
    actor_type: str,
    actor_id: int,
    config: Settings,
    *,
    now: datetime | None = None,
) -> tuple[int, ...]:
    instant = utc_now(now)
    with db.begin_nested():
        proposal = require_proposal(db, proposal_id, ministry_id, actor_type, actor_id, lock=True)
        items = db.scalars(
            select(RosterProposalItem)
            .where(RosterProposalItem.proposal_id == proposal.id)
            .order_by(RosterProposalItem.position)
        ).all()
        if proposal.status == "approved":
            return tuple(
                db.scalars(
                    select(AutomationInvitation.id)
                    .where(AutomationInvitation.proposal_item_id.in_([item.id for item in items]))
                    .order_by(AutomationInvitation.id)
                )
            )
        if proposal.status != "draft" or not items:
            raise RosterError("only nonempty draft proposals can be approved")
        # Total lock order: all volunteer IDs, then all slot IDs, both ascending.
        list(
            db.scalars(
                select(Volunteer)
                .where(Volunteer.id.in_({i.volunteer_id for i in items}))
                .order_by(Volunteer.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        )
        list(
            db.scalars(
                select(Slot)
                .where(Slot.id.in_({i.slot_id for i in items}))
                .order_by(Slot.id)
                .with_for_update()
                .execution_options(populate_existing=True)
            )
        )
        snapshot = proposal_snapshot(db, proposal)
        projected: list[AssignmentIntent] = []
        offers = []
        for item, suggestion in zip(items, snapshot.suggestions, strict=True):
            slot = db.get(Slot, item.slot_id)
            service = db.get(WorshipService, slot.service_id)
            if (
                service.ministry_id != ministry_id
                or service.service_date.replace(day=1) != proposal.month
            ):
                raise RosterError("proposal no longer matches its ministry and month")
            start = service_start(db, service)
            if start <= instant:
                raise RosterError("proposal contains a service that has already started")
            roster.check_assignment(db, item.volunteer_id, item.slot_id, projected=projected)
            if db.scalar(
                select(AutomationInvitation.id).where(
                    AutomationInvitation.volunteer_id == item.volunteer_id,
                    AutomationInvitation.slot_id == item.slot_id,
                    AutomationInvitation.status == "pending",
                )
            ):
                raise RosterError("a pending invitation already exists; review the proposal")
            offer = create_invitation(
                db,
                config,
                kind="distribution",
                source_key=f"distribution:{item.id}",
                proposal_item_id=item.id,
                volunteer_id=item.volunteer_id,
                slot_id=item.slot_id,
                reason=suggestion.score.explanation,
                now=instant,
            )
            offers.append(offer.id)
            projected.append(suggestion.intent)
        proposal.status, proposal.decided_at = "approved", instant
        ministry = authorize_actor(db, ministry_id, actor_type, actor_id)
        audit(
            db,
            ministry.church_id,
            actor_type,
            actor_id,
            "proposal_approved",
            "roster_proposal",
            proposal.id,
            {"invitations": offers},
        )
        return tuple(offers)


@dataclass(frozen=True)
class OfferDecision:
    status: str
    assignment_id: int | None = None


def respond_offer(
    db: Session,
    roster: RosterEngine,
    volunteer_id: int,
    offer_id: int,
    accept: bool,
    config: Settings,
    *,
    now: datetime | None = None,
) -> OfferDecision:
    instant = utc_now(now)
    with db.begin_nested():
        original = db.get(AutomationInvitation, offer_id)
        if original is None or original.volunteer_id != volunteer_id:
            raise NotAuthorized("invitation not found for this volunteer")
        member, slot = roster._lock_member_slot(db, volunteer_id, original.slot_id)
        offer = db.scalar(
            select(AutomationInvitation)
            .where(AutomationInvitation.id == offer_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if offer.status != "pending":
            return OfferDecision(offer.status, offer.assignment_id)
        service = db.get(WorshipService, slot.service_id)
        if service_fingerprint(db, slot) != offer.service_fingerprint:
            offer.status = "cancelled"
        elif database_utc(offer.expires_at) <= instant or service_start(db, service) <= instant:
            offer.status = "expired"
        elif not accept:
            offer.status = "declined"
        else:
            try:
                with db.begin_nested():
                    assignment_id = roster._assign_locked(db, member, slot, allow_published=True)
            except RosterError:
                offer.status = "cancelled"
            else:
                offer.status, offer.assignment_id = "accepted", assignment_id
                schedule = schedule_for(db, service.ministry_id, service.service_date)
                if schedule and schedule.state == "published":
                    publish_schedule(db, service.ministry_id, service.service_date)
        offer.responded_at = instant
        _update_waitlist(db, offer, instant)
        notify_offer(db, offer, offer.status, config)
        ministry = db.get(Ministry, service.ministry_id)
        audit(
            db,
            ministry.church_id,
            "volunteer",
            volunteer_id,
            f"automation_{offer.status}",
            "automation_invitation",
            offer.id,
            {"assignment_id": offer.assignment_id},
        )
        return OfferDecision(offer.status, offer.assignment_id)


def expire_offers(db: Session, config: Settings, *, now: datetime | None = None) -> int:
    instant = utc_now(now)
    offers = db.scalars(
        select(AutomationInvitation)
        .where(AutomationInvitation.status == "pending", AutomationInvitation.expires_at <= instant)
        .order_by(AutomationInvitation.id)
        .limit(100)
        .with_for_update(skip_locked=True)
    ).all()
    for offer in offers:
        offer.status, offer.responded_at = "expired", instant
        _update_waitlist(db, offer, instant)
        notify_offer(db, offer, "expired", config)
        slot = db.get(Slot, offer.slot_id)
        service = db.get(WorshipService, slot.service_id)
        ministry = db.get(Ministry, service.ministry_id)
        audit(
            db,
            ministry.church_id,
            "system",
            0,
            "automation_expired",
            "automation_invitation",
            offer.id,
        )
    return len(offers)


def _update_waitlist(db: Session, offer: AutomationInvitation, instant: datetime) -> None:
    if offer.kind != "waitlist":
        return
    entry = db.scalar(
        select(WaitlistEntry).where(WaitlistEntry.offer_id == offer.id).with_for_update()
    )
    if entry and entry.status == "offered":
        entry.status, entry.responded_at = offer.status, instant
