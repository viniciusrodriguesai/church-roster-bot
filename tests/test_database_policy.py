"""Real PostgreSQL timeout/pool recovery and fail-closed production configuration."""

import pytest
from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.exc import OperationalError, TimeoutError

from church_roster_bot.core.config import Settings
from church_roster_bot.core.database import database_engine


@pytest.mark.parametrize("url", ["sqlite://", "postgresql+psycopg://u:short@localhost/db"])
def test_production_rejects_insecure_configuration_without_exposing_url(url):
    with pytest.raises(ValidationError) as caught:
        Settings(_env_file=None, app_env="production", database_url=url)
    assert url not in str(caught.value)


def test_production_accepts_verified_tls_without_connecting():
    config = Settings(
        _env_file=None,
        app_env="production",
        database_url="postgresql+psycopg://app:FICTITIOUS_RANDOM_SECRET@localhost/db?sslmode=verify-full",
    )
    assert "FICTITIOUS_RANDOM_SECRET" not in repr(config)


def test_sqlite_factory_keeps_memory_database_usable():
    engine = database_engine(Settings(_env_file=None, database_url="sqlite://"))
    try:
        with engine.begin() as db:
            assert db.scalar(text("SELECT 1")) == 1
    finally:
        engine.dispose()


@pytest.mark.parametrize("scheme", ["postgres", "postgresql"])
def test_provider_urls_use_the_supported_driver(scheme):
    config = Settings(_env_file=None, database_url=f"{scheme}://example:fake@localhost/test")
    assert config.database_url.startswith("postgresql+psycopg://")


@pytest.mark.postgres
@pytest.mark.parametrize("roster", ["postgres"], indirect=True)
def test_native_timeout_rolls_back_and_pool_recovers(roster):
    original = roster[1].kw["bind"]
    with original.connect() as baseline:
        expected_schema = baseline.scalar(text("SELECT current_schema()"))
    native = str(original.url.query.get("options", "")) + " -c application_name=phase42_policy_test"
    config = Settings(
        _env_file=None,
        database_url=original.url.update_query_dict({"options": native}).render_as_string(
            hide_password=False
        ),
        db_statement_timeout_ms=50,
        db_pool_size=1,
        db_max_overflow=0,
        db_pool_timeout_seconds=0.05,
    )
    engine = database_engine(config)
    try:
        with engine.begin() as db:
            assert db.scalar(text("SHOW timezone")) == "UTC"
            assert db.scalar(text("SHOW transaction_isolation")) == "read committed"
            assert db.scalar(text("SELECT current_schema()")) == expected_schema
            assert db.scalar(text("SHOW application_name")) == "phase42_policy_test"
            with pytest.raises(TimeoutError):
                engine.connect()
        with pytest.raises(OperationalError), engine.begin() as db:
            db.execute(text("SELECT pg_sleep(0.2)"))
        with engine.begin() as db:
            assert db.scalar(text("SELECT 1")) == 1
    finally:
        engine.dispose()
