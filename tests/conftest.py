from datetime import time

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from church_roster_bot.core import models  # noqa: F401 - registers ORM tables
from church_roster_bot.core.database import Base
from church_roster_bot.members.models import Coordinator
from church_roster_bot.ministries.models import Church, Ministry, Role, RoleConflict
from church_roster_bot.scheduling.engine import RosterEngine
from church_roster_bot.services.models import Slot


@pytest.fixture
def roster():
    engine = create_engine(
        "sqlite+pysqlite:///:memory:",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    with factory.begin() as db:
        church = Church(name="Igreja Exemplo (fictícia)")
        db.add(church)
        db.flush()
        ministry = Ministry(church_id=church.id, name="Diaconato")
        db.add(ministry)
        db.flush()
        roles = [
            Role(ministry_id=ministry.id, name=name)
            for name in ("Abrir", "Fechar", "Ofertas", "Abrir e fechar")
        ]
        db.add_all(roles)
        db.flush()
        for role in (roles[0], roles[1], roles[3]):
            a, b = sorted((role.id, roles[2].id))
            db.add(RoleConflict(role_a_id=a, role_b_id=b))
        coordinator = Coordinator(
            church_id=church.id, name="Coordenação Exemplo", phone_e164="+15550100000"
        )
        db.add(coordinator)
        db.flush()
        ids = {
            "church": church.id,
            "ministry": ministry.id,
            "coordinator": coordinator.id,
            "open": roles[0].id,
            "close": roles[1].id,
            "offer": roles[2].id,
            "joint": roles[3].id,
        }
    yield RosterEngine(factory), factory, ids
    engine.dispose()


def make_service(engine: RosterEngine, factory, ministry_id, day, capacities):
    service_id = engine.create_service(ministry_id, day, time(19, 0), capacities)
    with factory() as db:
        slots = {
            slot.role_id: slot.id
            for slot in db.scalars(select(Slot).where(Slot.service_id == service_id))
        }
    return service_id, slots
