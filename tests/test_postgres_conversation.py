"""Verify the persistent chat transaction with real PostgreSQL in CI."""

import os
from datetime import date, time, timedelta

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from church_roster_bot.core.database import Base
from church_roster_bot.members.models import Coordinator, CoordinatorMinistry
from church_roster_bot.members.schemas import VolunteerCreate
from church_roster_bot.ministries.models import Church, Ministry, Role
from church_roster_bot.scheduling.engine import RosterEngine
from church_roster_bot.scheduling.lifecycle import schedule_for, transition_schedule
from church_roster_bot.scheduling.models import Assignment, ReplacementRequest
from church_roster_bot.scripts.simulate import simulate
from church_roster_bot.services.models import Slot
from church_roster_bot.whatsapp.models import InboundEvent


@pytest.mark.postgres
def test_chat_signup_commits_assignment_and_event_in_postgresql():
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("set TEST_DATABASE_URL to a disposable PostgreSQL database")
    if not url.startswith("postgresql"):
        pytest.fail("TEST_DATABASE_URL must point to PostgreSQL")
    engine = create_engine(url)
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    try:
        with factory.begin() as db:
            church = Church(name="Synthetic chat test church")
            db.add(church)
            db.flush()
            ministry = Ministry(church_id=church.id, name="Diaconato")
            db.add(ministry)
            db.flush()
            role = Role(ministry_id=ministry.id, name="Ofertas")
            db.add(role)
            db.flush()
            coordinator = Coordinator(
                church_id=church.id, name="Synthetic Coordinator", phone_e164="+15550100000"
            )
            db.add(coordinator)
            db.flush()
            db.add(CoordinatorMinistry(coordinator_id=coordinator.id, ministry_id=ministry.id))
            church_id, ministry_id, role_id = church.id, ministry.id, role.id
        roster = RosterEngine(factory)
        phone = "+15550100001"
        roster.register_volunteer(
            church_id,
            VolunteerCreate(name="Synthetic Volunteer", phone_e164=phone, role_ids={role_id}),
        )
        second_id = roster.register_volunteer(
            church_id,
            VolunteerCreate(
                name="Synthetic Replacement", phone_e164="+15550100002", role_ids={role_id}
            ),
        )
        day = date.today() + timedelta(days=1)
        service_id = roster.create_service(ministry_id, day, time(19), {role_id: 1})
        with factory.begin() as db:
            transition_schedule(schedule_for(db, ministry_id, day, create=True), "open")
            slot_id = db.scalar(select(Slot.id).where(Slot.service_id == service_id))
        for message in ("Oi", "1", str(service_id), str(slot_id), "SIM"):
            replies = simulate(phone, message, factory=factory)
        assert "CONFIRMADA" in replies[-1]["text"]["body"]
        with factory() as db:
            assignment = db.scalar(select(Assignment))
            assert assignment.status == "confirmed"
            assignment_id = assignment.id
            assert set(db.scalars(select(InboundEvent.status))) == {"processed"}
        for message in ("MENU", "4", str(assignment_id), "SIM"):
            simulate(phone, message, factory=factory)
        with factory() as db:
            assert db.get(Assignment, assignment_id).status == "replacement_requested"
            request_id = db.scalar(select(ReplacementRequest.id))
        for message in ("MENU", "7", f"SUBSTITUIR {request_id} {second_id}", "SIM"):
            replies = simulate("+15550100000", message, factory=factory)
        assert "Substituição confirmada" in replies[-1]["text"]["body"]
        with factory() as db:
            assert db.get(Assignment, assignment_id).status == "replaced"
            assert db.get(ReplacementRequest, request_id).status == "fulfilled"
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()
