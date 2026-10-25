"""Stable service identity and conversion of church civil times to UTC."""

import hashlib
import json
from datetime import datetime

from sqlalchemy.orm import Session

from church_roster_bot.core.time import local_to_utc
from church_roster_bot.ministries.models import Church, Ministry
from church_roster_bot.services.models import Slot, WorshipService


def service_start(db: Session, service: WorshipService) -> datetime:
    ministry = db.get(Ministry, service.ministry_id)
    church = db.get(Church, ministry.church_id)
    return local_to_utc(service.service_date, service.start_time, church.timezone)


def service_fingerprint(db: Session, slot: Slot) -> str:
    service = db.get(WorshipService, slot.service_id)
    ministry = db.get(Ministry, service.ministry_id)
    church = db.get(Church, ministry.church_id)
    values = (
        service.id,
        service.ministry_id,
        service.service_date.isoformat(),
        service.start_time.isoformat(),
        slot.role_id,
        church.timezone,
    )
    return hashlib.sha256(json.dumps(values).encode()).hexdigest()
