"""FIFO offers of available capacity; waiting and notification never assign a slot."""

from datetime import datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from church_roster_bot.core.config import Settings
from church_roster_bot.core.time import utc_now
from church_roster_bot.members.models import Volunteer
from church_roster_bot.ministries.models import Ministry
from church_roster_bot.notifications.automation import (
    notify_offer,
    service_fingerprint,
    service_start,
)
from church_roster_bot.scheduling.engine import OCCUPYING, RosterEngine
from church_roster_bot.scheduling.errors import NotAuthorized, RosterError
from church_roster_bot.scheduling.invitations import create_invitation
from church_roster_bot.scheduling.lifecycle import audit, schedule_for
from church_roster_bot.scheduling.models import Assignment, AutomationInvitation, WaitlistEntry
from church_roster_bot.services.models import Slot, WorshipService


def join_waitlist(
    db: Session,
    roster: RosterEngine,
    volunteer_id: int,
    slot_id: int,
    *,
    now: datetime | None = None,
) -> int:
    instant = utc_now(now)
    with db.begin_nested():
        member, slot = roster._lock_member_slot(db, volunteer_id, slot_id)
        existing = db.scalar(
            select(WaitlistEntry)
            .where(
                WaitlistEntry.volunteer_id == volunteer_id,
                WaitlistEntry.slot_id == slot_id,
                WaitlistEntry.status.in_(("waiting", "offered")),
            )
            .with_for_update()
        )
        if existing:
            return existing.id
        roster.check_assignment(
            db, member.id, slot.id, require_capacity=False, allow_published=True
        )
        service = db.get(WorshipService, slot.service_id)
        schedule = schedule_for(db, service.ministry_id, service.service_date)
        if schedule is None or schedule.state not in {"open", "published"}:
            raise RosterError("waiting list requires an open or published schedule")
        if service_start(db, service) <= instant:
            raise RosterError("service has already started")
        entry = WaitlistEntry(
            volunteer_id=member.id,
            slot_id=slot.id,
            created_at=instant,
            service_fingerprint=service_fingerprint(db, slot),
        )
        db.add(entry)
        db.flush()
        ministry = db.get(Ministry, service.ministry_id)
        audit(
            db,
            ministry.church_id,
            "volunteer",
            member.id,
            "waitlist_joined",
            "waitlist_entry",
            entry.id,
        )
        return entry.id


def cancel_waitlist(
    db: Session, roster: RosterEngine, volunteer_id: int, entry_id: int, config: Settings
) -> None:
    with db.begin_nested():
        original = db.get(WaitlistEntry, entry_id)
        if original is None or original.volunteer_id != volunteer_id:
            raise NotAuthorized("waitlist entry not found for this volunteer")
        member, slot = roster._lock_member_slot(db, volunteer_id, original.slot_id)
        current_offer_id = db.scalar(
            select(WaitlistEntry.offer_id).where(WaitlistEntry.id == entry_id)
        )
        offer = (
            db.get(
                AutomationInvitation, current_offer_id, with_for_update=True, populate_existing=True
            )
            if current_offer_id
            else None
        )
        entry = db.scalar(
            select(WaitlistEntry)
            .where(WaitlistEntry.id == entry_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if entry.status == "cancelled":
            return
        if entry.status not in {"waiting", "offered"}:
            raise RosterError("waitlist entry was already decided")
        entry.status, entry.responded_at = "cancelled", utc_now()
        if offer and offer.status == "pending":
            offer.status, offer.responded_at = "cancelled", entry.responded_at
            notify_offer(db, offer, "cancelled", config)
        service = db.get(WorshipService, slot.service_id)
        ministry = db.get(Ministry, service.ministry_id)
        audit(
            db,
            ministry.church_id,
            "volunteer",
            member.id,
            "waitlist_cancelled",
            "waitlist_entry",
            entry.id,
        )


def dispatch_waitlist(
    db: Session, roster: RosterEngine, config: Settings, *, now: datetime | None = None
) -> int:
    instant = utc_now(now)
    entries = db.scalars(
        select(WaitlistEntry)
        .where(WaitlistEntry.status.in_(("waiting", "offered")))
        .order_by(WaitlistEntry.created_at, WaitlistEntry.id)
    ).all()
    if not entries:
        return 0
    # Total member/slot lock order matches proposal approval and acceptance.
    list(
        db.scalars(
            select(Volunteer)
            .where(Volunteer.id.in_({e.volunteer_id for e in entries}))
            .order_by(Volunteer.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    )
    list(
        db.scalars(
            select(Slot)
            .where(Slot.id.in_({e.slot_id for e in entries}))
            .order_by(Slot.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    )
    current_offer_ids = set(
        db.scalars(
            select(WaitlistEntry.offer_id).where(
                WaitlistEntry.id.in_([entry.id for entry in entries]),
                WaitlistEntry.offer_id.is_not(None),
            )
        )
    )
    list(
        db.scalars(
            select(AutomationInvitation)
            .where(AutomationInvitation.id.in_(current_offer_ids))
            .order_by(AutomationInvitation.id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    )
    locked = db.scalars(
        select(WaitlistEntry)
        .where(WaitlistEntry.id.in_([e.id for e in entries]))
        .order_by(WaitlistEntry.created_at, WaitlistEntry.id)
        .with_for_update()
        .execution_options(populate_existing=True)
    ).all()
    created = 0
    for entry in locked:
        if entry.status == "offered":
            offer = db.get(AutomationInvitation, entry.offer_id)
            member = db.get(Volunteer, entry.volunteer_id)
            if offer.status == "pending" and (not member.active or not member.invite_opt_in):
                offer.status, offer.responded_at = "cancelled", instant
                notify_offer(db, offer, "cancelled", config)
            if offer.status != "pending":
                entry.status, entry.responded_at = offer.status, instant
            continue
        if entry.status != "waiting":
            continue
        member = db.get(Volunteer, entry.volunteer_id)
        slot = db.get(Slot, entry.slot_id)
        service = db.get(WorshipService, slot.service_id)
        try:
            if (
                service_start(db, service) <= instant
                or service_fingerprint(db, slot) != entry.service_fingerprint
            ):
                raise RosterError("service changed or has already started")
            roster.check_assignment(
                db, member.id, slot.id, require_capacity=False, allow_published=True
            )
        except (ValueError, RosterError):
            entry.status, entry.responded_at = "ineligible", instant
            ministry = db.get(Ministry, service.ministry_id)
            audit(
                db,
                ministry.church_id,
                "system",
                0,
                "waitlist_ineligible",
                "waitlist_entry",
                entry.id,
            )
            continue
        if not member.invite_opt_in:
            continue
        occupied = (
            db.scalar(
                select(func.count(Assignment.id)).where(
                    Assignment.slot_id == slot.id, Assignment.status.in_(OCCUPYING)
                )
            )
            or 0
        )
        pending = (
            db.scalar(
                select(func.count(AutomationInvitation.id)).where(
                    AutomationInvitation.slot_id == slot.id,
                    AutomationInvitation.kind == "waitlist",
                    AutomationInvitation.status == "pending",
                )
            )
            or 0
        )
        if occupied + pending >= slot.capacity:
            continue
        if db.scalar(
            select(AutomationInvitation.id).where(
                AutomationInvitation.volunteer_id == member.id,
                AutomationInvitation.slot_id == slot.id,
                AutomationInvitation.status == "pending",
            )
        ):
            continue
        offer = create_invitation(
            db,
            config,
            kind="waitlist",
            source_key=f"waitlist:{entry.id}",
            volunteer_id=member.id,
            slot_id=slot.id,
            reason="Sua vez na lista de espera: surgiu uma vaga elegível.",
            now=instant,
        )
        entry.status, entry.offer_id = "offered", offer.id
        ministry = db.get(Ministry, service.ministry_id)
        audit(
            db,
            ministry.church_id,
            "system",
            0,
            "waitlist_offered",
            "waitlist_entry",
            entry.id,
            {"invitation_id": offer.id},
        )
        created += 1
    return created
