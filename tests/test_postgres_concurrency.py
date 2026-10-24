"""Exercise the row lock with a real PostgreSQL server when configured."""

import os
from concurrent.futures import ThreadPoolExecutor
from datetime import date, time
from threading import Barrier

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from church_roster_bot.core import models  # noqa: F401 - register tables
from church_roster_bot.core.database import Base
from church_roster_bot.members.schemas import VolunteerCreate
from church_roster_bot.ministries.models import Church, Ministry, Role
from church_roster_bot.scheduling.engine import RosterEngine
from church_roster_bot.scheduling.errors import NoCapacity
from church_roster_bot.scheduling.models import Assignment
from church_roster_bot.services.models import Slot


@pytest.mark.postgres
def test_simultaneous_claims_cannot_overbook_last_slot():
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("set TEST_DATABASE_URL to a disposable PostgreSQL database")
    if not url.startswith("postgresql"):
        pytest.fail("TEST_DATABASE_URL must point to PostgreSQL")
    db_engine = create_engine(url)
    Base.metadata.create_all(db_engine)
    factory = sessionmaker(db_engine, expire_on_commit=False)
    try:
        with factory.begin() as db:
            church = Church(name="Concurrent test church")
            db.add(church)
            db.flush()
            ministry = Ministry(church_id=church.id, name="Diaconato")
            db.add(ministry)
            db.flush()
            role = Role(ministry_id=ministry.id, name="Ofertas")
            db.add(role)
            db.flush()
            church_id, ministry_id, role_id = church.id, ministry.id, role.id
        roster = RosterEngine(factory)
        first = roster.register_volunteer(
            church_id,
            VolunteerCreate(name="Example One", phone_e164="+15550100001", role_ids={role_id}),
        )
        second = roster.register_volunteer(
            church_id,
            VolunteerCreate(name="Example Two", phone_e164="+15550100002", role_ids={role_id}),
        )
        service_id = roster.create_service(ministry_id, date(2026, 11, 1), time(19), {role_id: 1})
        with factory() as db:
            slot_id = db.scalar(select(Slot.id).where(Slot.service_id == service_id))
        barrier = Barrier(2)

        def claim(volunteer_id: int) -> bool:
            barrier.wait(timeout=10)
            try:
                roster.assign(volunteer_id, slot_id)
                return True
            except NoCapacity:
                return False

        with ThreadPoolExecutor(max_workers=2) as pool:
            outcomes = list(pool.map(claim, (first, second)))
        assert sorted(outcomes) == [False, True]
        with factory() as db:
            assert db.scalar(select(func.count(Assignment.id))) == 1
    finally:
        Base.metadata.drop_all(db_engine)
        db_engine.dispose()
