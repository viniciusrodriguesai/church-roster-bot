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
    MonthlySchedule,
    ReplacementRequest,
    RosterProposal,
    RosterProposalItem,
    ThirdDayRequest,
)
from church_roster_bot.services.models import Slot, WorshipService
from church_roster_bot.whatsapp.models import InboundEvent, OutboundMessage

__all__ = [
    "Assignment",
    "AuditEvent",
    "Church",
    "ConversationSession",
    "Coordinator",
    "CoordinatorMinistry",
    "GeneralAdmin",
    "InboundEvent",
    "Ministry",
    "MonthlySchedule",
    "OutboundMessage",
    "ReplacementRequest",
    "RosterProposal",
    "RosterProposalItem",
    "Role",
    "RoleConflict",
    "ScheduleVersion",
    "Slot",
    "ThirdDayRequest",
    "Unavailability",
    "Volunteer",
    "VolunteerRole",
    "WorshipService",
]
