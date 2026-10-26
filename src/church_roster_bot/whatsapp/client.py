"""Official Meta Graph API transport; no conversation rules live here."""

import httpx

from church_roster_bot.core.config import Settings


class CredentialsMissingError(RuntimeError):
    """A known pre-send failure: no request was attempted."""


def provider_error_code(value: object) -> str:
    """Only numeric provider codes may reach logs and coordinator notices."""
    if type(value) is int:
        value = str(value)
    if isinstance(value, str) and 0 < len(value) <= 20 and value.isascii() and value.isdecimal():
        return value
    return "unknown"


class MetaSendError(Exception):
    def __init__(self, status_code: int, code: str = "unknown"):
        super().__init__(f"Meta send failed: HTTP {status_code}, code {code}")
        self.status_code = status_code
        self.code = code


class WhatsAppClient:
    def __init__(self, settings: Settings, transport: httpx.BaseTransport | None = None):
        self.settings = settings
        self.transport = transport

    def send(self, recipient: str, payload: dict) -> str:
        token = self.settings.whatsapp_access_token.get_secret_value()
        if not token or not self.settings.whatsapp_phone_number_id:
            raise CredentialsMissingError("WhatsApp credentials are not configured")
        url = (
            f"https://graph.facebook.com/{self.settings.whatsapp_api_version}/"
            f"{self.settings.whatsapp_phone_number_id}/messages"
        )
        body = {
            "messaging_product": "whatsapp",
            "recipient_type": "individual",
            "to": recipient.lstrip("+"),
            **payload,
        }
        with httpx.Client(timeout=10, transport=self.transport) as client:
            response = client.post(
                url,
                json=body,
                headers={"Authorization": f"Bearer {token}"},
            )
        if response.status_code >= 400:
            try:
                result = response.json()
            except ValueError:
                result = None
            error = result.get("error") if isinstance(result, dict) else None
            code = provider_error_code(error.get("code")) if isinstance(error, dict) else "unknown"
            raise MetaSendError(response.status_code, code)
        try:
            ident = response.json()["messages"][0]["id"]
            if not isinstance(ident, str) or not ident.strip() or len(ident) > 255:
                raise ValueError("invalid message identity")
            return ident
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            raise MetaSendError(response.status_code, "invalid_response") from exc
