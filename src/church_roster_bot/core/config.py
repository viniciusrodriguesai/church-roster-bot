import re
from typing import Literal

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url


class Settings(BaseSettings):
    database_url: str = Field(
        default="postgresql+psycopg://roster:change-me@localhost:5432/roster", repr=False
    )
    app_env: Literal["development", "test", "production"] = "development"
    db_connect_timeout_seconds: int = Field(default=5, ge=1, le=60)
    db_statement_timeout_ms: int = Field(default=30000, ge=1, le=3600000)
    db_lock_timeout_ms: int = Field(default=5000, ge=1, le=60000)
    db_idle_transaction_timeout_ms: int = Field(default=60000, ge=1000, le=3600000)
    db_pool_size: int = Field(default=5, ge=1, le=50)
    db_max_overflow: int = Field(default=5, ge=0, le=50)
    db_pool_timeout_seconds: float = Field(default=10, ge=0.05, le=60)
    db_pool_recycle_seconds: int = Field(default=1800, ge=30, le=86400)
    whatsapp_access_token: SecretStr = SecretStr("")
    whatsapp_phone_number_id: str = ""
    whatsapp_verify_token: SecretStr = SecretStr("")
    meta_app_secret: SecretStr = SecretStr("")
    whatsapp_api_version: str = "v25.0"
    whatsapp_invite_template: str = ""
    whatsapp_notification_template: str = ""
    whatsapp_automation_template: str = ""
    whatsapp_reminder_template: str = ""
    automation_invite_ttl_hours: int = Field(default=48, ge=1, le=168)
    whatsapp_template_language: str = "pt_BR"
    conversation_ttl_minutes: int = Field(default=30, ge=1, le=1440)
    webhook_max_bytes: int = Field(default=262144, ge=1024)
    webhook_max_events: int = Field(default=1000, ge=1, le=10000)
    webhook_read_timeout_seconds: float = Field(default=5.0, ge=0.01, le=60)
    health_ready_timeout_seconds: float = Field(default=2.0, ge=0.05, le=10)
    inbound_limit_per_minute: int = Field(default=30, ge=1)

    @field_validator("whatsapp_api_version")
    @classmethod
    def valid_api_version(cls, value: str) -> str:
        if not re.fullmatch(r"v\d+\.0", value):
            raise ValueError("WhatsApp API version must look like v25.0")
        return value

    model_config = SettingsConfigDict(env_file=".env", extra="ignore", hide_input_in_errors=True)

    @model_validator(mode="after")
    def valid_database_policy(self):
        try:
            url = make_url(self.database_url)
        except Exception as exc:
            raise ValueError("invalid database configuration") from exc
        if url.drivername in {"postgres", "postgresql"}:
            url = url.set(drivername="postgresql+psycopg")
            self.database_url = url.render_as_string(hide_password=False)
        if url.drivername not in {"sqlite", "sqlite+pysqlite", "postgresql+psycopg"}:
            raise ValueError("database driver must be SQLite or PostgreSQL with psycopg")
        if self.app_env == "production":
            if url.drivername != "postgresql+psycopg" or url.query.get("sslmode") != "verify-full":
                raise ValueError("production requires PostgreSQL with TLS verify-full")
            if not url.username or not url.password or len(url.password) < 16:
                raise ValueError("production requires dedicated credentials and a strong password")
            if url.password in {
                "change-me",
                "test-only-password",
                "replace-with-generated-password",
            }:
                raise ValueError("example credentials cannot be used in production")
        return self


settings = Settings()
