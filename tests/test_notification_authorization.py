"""Queued administrative alerts must not survive revoked access or a phone change."""

import pytest
from sqlalchemy import delete, select

from church_roster_bot.core.config import Settings
from church_roster_bot.members.models import Coordinator, CoordinatorMinistry
from church_roster_bot.ministries.models import Church
from church_roster_bot.notifications.coordinators import notify_ministry
from church_roster_bot.notifications.worker import Worker
from church_roster_bot.scripts.simulate import simulate
from church_roster_bot.whatsapp.models import OutboundMessage
from church_roster_bot.whatsapp.payloads import text_payload
from tests.test_invitation_delivery import RecordingClient, confirm, prepare
from tests.test_phase2 import COORD, FIRST

CONFIG = Settings(_env_file=None, whatsapp_notification_template="fictitious_template")
PRIVATE_BODY = "FICTITIOUS_PRIVATE_ROSTER"


@pytest.mark.parametrize(
    "roster", ["sqlite_file", pytest.param("postgres", marks=pytest.mark.postgres)], indirect=True
)
class TestCoordinatorNoticeAuthorization:
    @pytest.mark.parametrize("change", ["inactive", "revoked", "phone", "church"])
    def test_scope_and_recipient_are_rechecked_at_claim(self, roster, change):
        _, factory, ids = roster
        with factory.begin() as db:
            assert (
                notify_ministry(db, ids["ministry"], "security:notice", PRIVATE_BODY, CONFIG) == 1
            )
        with factory.begin() as db:
            coordinator = db.get(Coordinator, ids["coordinator"])
            if change == "inactive":
                coordinator.active = False
            elif change == "revoked":
                db.execute(
                    delete(CoordinatorMinistry).where(
                        CoordinatorMinistry.coordinator_id == coordinator.id
                    )
                )
            elif change == "phone":
                coordinator.phone_e164 = "+15550100999"
            else:
                church = Church(name="Outra igreja fictícia")
                db.add(church)
                db.flush()
                coordinator.church_id = church.id
        client = RecordingClient()
        worker = Worker(factory, CONFIG, client)
        assert worker.send_one()
        assert not worker.send_one()
        assert client.calls == []
        with factory() as db:
            outgoing = db.scalar(select(OutboundMessage))
            assert (outgoing.status, outgoing.attempts) == ("cancelled", 0)

    def test_legacy_exception_notice_checks_current_coordinator_permissions(self, roster):
        _, factory, ids = roster
        _, _, config = prepare(roster)
        invitation_id, _ = confirm(factory, config)
        with factory.begin() as db:
            db.execute(delete(OutboundMessage))
            db.get(Coordinator, ids["coordinator"]).active = False
        simulate(FIRST, f"ACEITAR {invitation_id}", factory=factory, config=config)
        client = RecordingClient()
        worker = Worker(factory, config, client)
        while worker.send_one():
            pass
        assert not any(phone == COORD for phone, _ in client.calls)

    def test_legacy_proactive_notice_without_scope_is_preserved_but_not_sent(self, roster):
        factory = roster[1]
        with factory.begin() as db:
            db.add(
                OutboundMessage(
                    recipient_e164=COORD,
                    dedupe_key="legacy:administrative-notice",
                    payload=text_payload(PRIVATE_BODY),
                )
            )
        client = RecordingClient()
        worker = Worker(factory, CONFIG, client)
        assert worker.send_one()
        assert not worker.send_one()
        assert not client.calls
        with factory() as db:
            outgoing = db.scalar(select(OutboundMessage))
            assert outgoing.payload == text_payload(PRIVATE_BODY)
            assert (outgoing.status, outgoing.attempts) == ("cancelled", 0)

    def test_authorized_notice_is_delivered_once(self, roster):
        _, factory, ids = roster
        with factory.begin() as db:
            assert (
                notify_ministry(db, ids["ministry"], "security:notice", PRIVATE_BODY, CONFIG) == 1
            )
            db.flush()
            assert (
                notify_ministry(db, ids["ministry"], "security:notice", PRIVATE_BODY, CONFIG) == 0
            )
        client = RecordingClient()
        worker = Worker(factory, CONFIG, client)
        assert worker.send_one()
        worker.recover_sending()
        assert not worker.send_one()
        assert len(client.calls) == 1 and client.calls[0][0] == COORD
        assert PRIVATE_BODY in str(client.calls[0][1])
