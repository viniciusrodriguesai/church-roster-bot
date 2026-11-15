"""The VPS rehearsal must never contact Meta, even with accidental credentials."""

import httpx
import pytest

from church_roster_bot.core.config import Settings
from church_roster_bot.notifications.worker import Worker
from church_roster_bot.whatsapp.client import CredentialsMissingError, WhatsAppClient


def test_disabled_delivery_blocks_before_any_network_request():
    def reject_request(request):
        pytest.fail("disabled delivery attempted an HTTP request")

    config = Settings(
        _env_file=None,
        database_url="sqlite://",
        whatsapp_delivery_enabled=False,
        whatsapp_access_token="fictitious-token",
        whatsapp_phone_number_id="fictitious-id",
    )
    client = WhatsAppClient(config, transport=httpx.MockTransport(reject_request))
    with pytest.raises(CredentialsMissingError, match="disabled"):
        client.send("+15550100001", {"type": "text", "text": {"body": "test"}})


def test_worker_finishes_current_cycle_before_stopping(monkeypatch, tmp_path):
    heartbeat = tmp_path / "worker.heartbeat"
    worker = Worker(config=Settings(_env_file=None, worker_heartbeat_file=heartbeat))
    calls = []
    monkeypatch.setattr(worker, "recover_sending", lambda: calls.append("recovery"))
    monkeypatch.setattr(worker, "maintain", lambda: False)

    def process():
        calls.append("process")
        worker.request_stop()
        return True

    monkeypatch.setattr(worker, "process_one", process)
    monkeypatch.setattr(worker, "send_one", lambda: calls.append("finish_delivery"))
    worker.run_forever()
    assert calls == ["recovery", "process", "finish_delivery"]
    assert heartbeat.exists()
