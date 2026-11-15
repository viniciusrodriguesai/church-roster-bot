"""The VPS rehearsal must never contact Meta, even with accidental credentials."""

import httpx
import pytest

from church_roster_bot.core.config import Settings
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
