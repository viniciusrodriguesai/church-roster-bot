"""Import all ORM models so Alembic sees every table."""

from church_roster_bot.audit.models import AuditEvent, ScheduleVersion
from church_roster_bot.availability.models import Unavailability
from church_roster_bot.conversations.models import ConversationSession
from church_roster_bot.members.models import (
    Coordinator,
    CoordinatorMinistry,
    GeneralAdmin,
    Volunteer,
    VolunteerRole,
)
from church_roster_bot.ministries.models import Church, Ministry, Role, RoleConflict
from church_roster_bot.scheduling.models import (
    Assignment,
    AutomationInvitation,
    AutomationPolicy,
    EmergencyPolicy,
    MonthlySchedule,
    ReminderPolicy,
    ReplacementRequest,
    RosterProposal,
    RosterProposalItem,
    ThirdDayRequest,
    WaitlistEntry,
)
from church_roster_bot.services.models import Slot, WorshipService
from church_roster_bot.whatsapp.models import InboundEvent, OutboundMessage

__all__ = [
    "Assignment",
    "AutomationInvitation",
    "AutomationPolicy",
    "AuditEvent",
    "Church",
    "ConversationSession",
    "Coordinator",
    "CoordinatorMinistry",
    "EmergencyPolicy",
    "GeneralAdmin",
    "InboundEvent",
    "Ministry",
    "MonthlySchedule",
    "OutboundMessage",
    "ReminderPolicy",
    "ReplacementRequest",
    "RosterProposal",
    "RosterProposalItem",
    "Role",
    "RoleConflict",
    "ScheduleVersion",
    "Slot",
    "ThirdDayRequest",
    "WaitlistEntry",
    "Unavailability",
    "Volunteer",
    "VolunteerRole",
    "WorshipService",
]
