"""Programmatic migrations must preserve application diagnostics."""

import logging

from alembic.config import Config

from alembic import command
from church_roster_bot.core.config import settings


def test_migrations_do_not_disable_application_loggers(migration_url, monkeypatch):
    monkeypatch.setattr(settings, "database_url", migration_url)
    application = logging.getLogger("church_roster_bot.core.health")
    application.disabled = False
    command.upgrade(Config("alembic.ini"), "head")
    assert not application.disabled


def test_embedded_migration_can_preserve_the_callers_logging_configuration(
    migration_url, monkeypatch
):
    monkeypatch.setattr(settings, "database_url", migration_url)
    config = Config("alembic.ini")
    config.attributes["configure_logger"] = False
    original = list(logging.getLogger().handlers)
    command.upgrade(config, "head")
    assert logging.getLogger().handlers == original
