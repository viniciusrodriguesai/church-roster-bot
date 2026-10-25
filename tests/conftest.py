import os
from datetime import time
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from church_roster_bot.core import models  # noqa: F401 - registers ORM tables
from church_roster_bot.core.database import Base
from church_roster_bot.members.models import Coordinator, CoordinatorMinistry
from church_roster_bot.ministries.models import Church, Ministry, Role, RoleConflict
from church_roster_bot.scheduling.engine import RosterEngine
from church_roster_bot.services.models import Slot


@pytest.fixture
def roster(request, tmp_path):
    postgres = getattr(request, "param", "sqlite") == "postgres"
    if postgres:
        url = os.getenv("TEST_DATABASE_URL")
        if not url:
            pytest.skip("set TEST_DATABASE_URL to a disposable PostgreSQL database")
        if not url.startswith("postgresql"):
            pytest.fail("TEST_DATABASE_URL must point to PostgreSQL")
        engine = create_engine(url)
    elif getattr(request, "param", "sqlite") == "sqlite_file":
        engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'terminal.sqlite3'}")
    else:
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
        db.add(CoordinatorMinistry(coordinator_id=coordinator.id, ministry_id=ministry.id))
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
    if postgres:
        Base.metadata.drop_all(engine)
    engine.dispose()


def make_service(engine: RosterEngine, factory, ministry_id, day, capacities):
    service_id = engine.create_service(ministry_id, day, time(19, 0), capacities)
    with factory() as db:
        slots = {
            slot.role_id: slot.id
            for slot in db.scalars(select(Slot).where(Slot.service_id == service_id))
        }
    return service_id, slots


@pytest.fixture(params=["sqlite", pytest.param("postgres", marks=pytest.mark.postgres)])
def migration_url(request, tmp_path):
    """Each PostgreSQL migration round trip owns a separate disposable schema."""
    if request.param == "sqlite":
        yield f"sqlite+pysqlite:///{tmp_path / 'migration.sqlite3'}"
        return
    url = os.getenv("TEST_DATABASE_URL")
    if not url:
        pytest.skip("set TEST_DATABASE_URL to a disposable PostgreSQL database")
    if not url.startswith("postgresql"):
        pytest.fail("TEST_DATABASE_URL must point to PostgreSQL")
    schema = f"migration_{uuid4().hex}"
    engine = create_engine(url)
    with engine.begin() as db:
        db.exec_driver_sql(f'CREATE SCHEMA "{schema}"')
    try:
        yield (
            make_url(url)
            .update_query_dict({"options": f"-csearch_path={schema}"})
            .render_as_string(hide_password=False)
        )
    finally:
        with engine.begin() as db:
            db.exec_driver_sql(f'DROP SCHEMA "{schema}" CASCADE')
        engine.dispose()
