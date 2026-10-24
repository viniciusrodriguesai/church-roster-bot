"""Import all ORM models so Alembic sees every table."""

from church_roster_bot.availability.models import Unavailability
from church_roster_bot.members.models import Coordinator, Volunteer, VolunteerRole
from church_roster_bot.ministries.models import Church, Ministry, Role, RoleConflict
from church_roster_bot.scheduling.models import Assignment, ThirdDayRequest
from church_roster_bot.services.models import Slot, WorshipService

__all__ = [
    "Assignment",
    "Church",
    "Coordinator",
    "Ministry",
    "Role",
    "RoleConflict",
    "Slot",
    "ThirdDayRequest",
    "Unavailability",
    "Volunteer",
    "VolunteerRole",
    "WorshipService",
]
