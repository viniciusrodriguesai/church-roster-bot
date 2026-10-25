"""Capture ministry provenance at write time without guessing ambiguous legacy ownership."""

from sqlalchemy.orm import Session

from church_roster_bot.audit.models import ScheduleVersion
from church_roster_bot.ministries.models import Ministry, Role
from church_roster_bot.scheduling.models import (
    Assignment,
    AutomationInvitation,
    MonthlySchedule,
    ReplacementRequest,
    RosterProposal,
    ThirdDayRequest,
    WaitlistEntry,
)
from church_roster_bot.services.models import Slot, WorshipService


def entity_ministry(db: Session, entity_type: str, entity_id: int) -> int | None:
    if entity_type == "ministry":
        return entity_id if db.get(Ministry, entity_id) else None
    direct = {
        "service": WorshipService,
        "role": Role,
        "schedule": MonthlySchedule,
        "roster_proposal": RosterProposal,
    }
    if entity_type in direct:
        row = db.get(direct[entity_type], entity_id)
        return row.ministry_id if row else None
    if entity_type == "schedule_version":
        version = db.get(ScheduleVersion, entity_id)
        schedule = db.get(MonthlySchedule, version.schedule_id) if version else None
        return schedule.ministry_id if schedule else None
    slots = {
        "assignment": Assignment,
        "automation_invitation": AutomationInvitation,
        "third_day_request": ThirdDayRequest,
        "waitlist_entry": WaitlistEntry,
    }
    slot_id = entity_id if entity_type == "slot" else None
    if entity_type in slots:
        row = db.get(slots[entity_type], entity_id)
        slot_id = row.slot_id if row else None
    if entity_type == "replacement_request":
        request = db.get(ReplacementRequest, entity_id)
        assignment = db.get(Assignment, request.assignment_id) if request else None
        slot_id = assignment.slot_id if assignment else None
    if slot_id:
        slot = db.get(Slot, slot_id)
        service = db.get(WorshipService, slot.service_id) if slot else None
        return service.ministry_id if service else None
    return None
