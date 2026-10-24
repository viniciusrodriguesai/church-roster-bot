import re

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    database_url: str = "postgresql+psycopg://roster:change-me@localhost:5432/roster"
    app_env: str = "development"
    whatsapp_access_token: SecretStr = SecretStr("")
    whatsapp_phone_number_id: str = ""
    whatsapp_verify_token: SecretStr = SecretStr("")
    meta_app_secret: SecretStr = SecretStr("")
    whatsapp_api_version: str = "v25.0"
    whatsapp_invite_template: str = ""
    whatsapp_template_language: str = "pt_BR"
    conversation_ttl_minutes: int = Field(default=30, ge=1, le=1440)
    webhook_max_bytes: int = Field(default=262144, ge=1024)
    inbound_limit_per_minute: int = Field(default=30, ge=1)

    @field_validator("whatsapp_api_version")
    @classmethod
    def valid_api_version(cls, value: str) -> str:
        if not re.fullmatch(r"v\d+\.0", value):
            raise ValueError("WhatsApp API version must look like v25.0")
        return value

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


settings = Settings()
