"""Legacy numeric invitations cannot assign or send after the church's service start."""

from datetime import datetime, timedelta

import pytest
from sqlalchemy import func, select

from church_roster_bot.administration.service import edit_service
from church_roster_bot.core import time as clock
from church_roster_bot.ministries.models import Church
from church_roster_bot.scheduling.errors import RosterError
from church_roster_bot.scheduling.models import Assignment, ThirdDayRequest
from church_roster_bot.scripts.simulate import simulate
from church_roster_bot.services.models import Slot, WorshipService
from church_roster_bot.services.timing import service_start
from church_roster_bot.whatsapp.models import OutboundMessage
from tests.test_invitation_delivery import (
    RecordingClient,
    confirm,
    drain,
    outgoing_for,
    prepare,
)
from tests.test_phase2 import FIRST, bodies


def freeze(monkeypatch, instant):
    class ControlledDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return instant.astimezone(tz) if tz else instant.replace(tzinfo=None)

    monkeypatch.setattr(clock, "datetime", ControlledDateTime)


def start_for(factory, slot_id):
    with factory() as db:
        return service_start(db, db.get(WorshipService, db.get(Slot, slot_id).service_id))


@pytest.mark.parametrize(
    "roster", ["sqlite_file", pytest.param("postgres", marks=pytest.mark.postgres)], indirect=True
)
class TestExceptionalDeadlines:
    @pytest.mark.parametrize("change", ["date", "time", "timezone"])
    def test_old_reply_cannot_authorize_a_changed_service(self, roster, change):
        _, factory, ids = roster
        _, slot_id, config = prepare(roster)
        invitation_id, _ = confirm(factory, config)
        with factory.begin() as db:
            service = db.get(WorshipService, db.get(Slot, slot_id).service_id)
            if change == "timezone":
                db.get(Church, ids["church"]).timezone = "UTC"
            else:
                value = (
                    (service.service_date + timedelta(days=1)).isoformat()
                    if change == "date"
                    else "20:00"
                )
                edit_service(
                    db,
                    ids["ministry"],
                    service.id,
                    change,
                    value,
                    "coordinator",
                    ids["coordinator"],
                )
        response = bodies(
            simulate(FIRST, f"ACEITAR {invitation_id}", factory=factory, config=config)
        )
        assert "desatualizado" in response
        with factory() as db:
            assert db.scalar(select(func.count(Assignment.id))) == 2
            assert db.get(ThirdDayRequest, invitation_id).status == "pending"

    def test_legacy_request_without_snapshot_requires_new_authorization(self, roster):
        _, factory, _ = roster
        _, _, config = prepare(roster)
        invitation_id, _ = confirm(factory, config)
        with factory.begin() as db:
            db.get(ThirdDayRequest, invitation_id).service_fingerprint = None
        response = bodies(
            simulate(FIRST, f"ACEITAR {invitation_id}", factory=factory, config=config)
        )
        assert "desatualizado" in response
        with factory() as db:
            assert db.scalar(select(func.count(Assignment.id))) == 2

    def test_cached_timezone_cannot_hide_a_changed_invitation(self, roster):
        engine, factory, ids = roster
        member, _, config = prepare(roster)
        invitation_id, _ = confirm(factory, config)
        with factory.begin() as stale:
            church = stale.get(Church, ids["church"])
            with factory.begin() as other:
                other.get(Church, ids["church"]).timezone = "UTC"
            assert church.timezone != "UTC"
            with pytest.raises(RosterError, match="desatualizado"):
                engine.respond_third_day(member, invitation_id, True, db=stale)
        with factory() as db:
            assert db.scalar(select(func.count(Assignment.id))) == 2

    def test_coordinator_can_reauthorize_changed_service_without_rewriting_history(self, roster):
        engine, factory, ids = roster
        member, slot_id, config = prepare(roster)
        original_id, _ = confirm(factory, config)
        with factory.begin() as db:
            service = db.get(WorshipService, db.get(Slot, slot_id).service_id)
            edit_service(
                db, ids["ministry"], service.id, "time", "20:00", "coordinator", ids["coordinator"]
            )
        new_id = engine.request_third_day(ids["coordinator"], member, slot_id, "Nova autorização")
        assert new_id != original_id
        assert engine.request_third_day(ids["coordinator"], member, slot_id, "Repetida") == new_id
        with factory() as db:
            assert db.get(ThirdDayRequest, original_id).justification == "Necessidade pontual"
            assert db.get(ThirdDayRequest, new_id).justification == "Nova autorização"
        assert engine.respond_third_day(member, new_id, True) is not None

    def test_changed_service_cancels_queued_exception(self, roster):
        _, factory, ids = roster
        _, slot_id, config = prepare(roster)
        invitation_id, _ = confirm(factory, config)
        with factory.begin() as db:
            service = db.get(WorshipService, db.get(Slot, slot_id).service_id)
            edit_service(
                db, ids["ministry"], service.id, "time", "20:00", "coordinator", ids["coordinator"]
            )
        client = drain(factory, config, RecordingClient())
        assert outgoing_for(factory, invitation_id).status == "cancelled"
        assert not any(
            phone == FIRST and p.get("type") == "interactive" for phone, p in client.calls
        )

    def test_coordinator_cannot_create_an_exception_for_a_started_service(
        self, roster, monkeypatch
    ):
        engine, factory, ids = roster
        member, slot, _ = prepare(roster)
        freeze(monkeypatch, start_for(factory, slot))
        with pytest.raises(RosterError, match="expirado"):
            engine.request_third_day(ids["coordinator"], member, slot, "Cobertura fictícia")
        with factory() as db:
            assert db.scalar(select(func.count(ThirdDayRequest.id))) == 0

    @pytest.mark.parametrize("seconds", [-1, 0, 1])
    def test_numeric_reply_rechecks_service_start_atomically(self, roster, monkeypatch, seconds):
        _, factory, _ = roster
        _, slot, config = prepare(roster)
        invitation_id, _ = confirm(factory, config)
        instant = start_for(factory, slot) + timedelta(seconds=seconds)
        freeze(monkeypatch, instant)
        response = bodies(
            simulate(FIRST, f"ACEITAR {invitation_id}", factory=factory, config=config)
        )
        with factory() as db:
            request = db.get(ThirdDayRequest, invitation_id)
            if seconds < 0:
                assert "confirmada" in response
                assert request.status == "accepted"
                assert db.scalar(select(func.count(Assignment.id))) == 3
            else:
                assert "expirado" in response
                assert (request.status, request.responded_at) == ("pending", None)
                assert db.scalar(select(func.count(Assignment.id))) == 2
                assert (
                    db.scalar(
                        select(OutboundMessage.id).where(
                            OutboundMessage.dedupe_key == f"invitation:{invitation_id}:aceito"
                        )
                    )
                    is None
                )

    def test_volunteer_can_still_decline_an_elapsed_invitation(self, roster, monkeypatch):
        _, factory, _ = roster
        _, slot, config = prepare(roster)
        invitation_id, _ = confirm(factory, config)
        freeze(monkeypatch, start_for(factory, slot) + timedelta(hours=1))
        assert "recusado" in bodies(
            simulate(FIRST, f"RECUSAR {invitation_id}", factory=factory, config=config)
        )
        with factory() as db:
            assert db.get(ThirdDayRequest, invitation_id).status == "declined"
            assert db.scalar(select(func.count(Assignment.id))) == 2

    def test_worker_does_not_send_an_exception_after_start(self, roster, monkeypatch):
        _, factory, _ = roster
        _, slot, config = prepare(roster)
        invitation_id, _ = confirm(factory, config)
        freeze(monkeypatch, start_for(factory, slot) + timedelta(seconds=1))
        client = drain(factory, config, RecordingClient())
        outgoing = outgoing_for(factory, invitation_id)
        assert (outgoing.status, outgoing.attempts) == ("cancelled", 0)
        assert not any(
            phone == FIRST and payload.get("type") == "interactive"
            for phone, payload in client.calls
        )
