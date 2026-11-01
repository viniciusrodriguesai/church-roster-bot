import logging
import sqlite3
import time
from collections.abc import Callable, Iterator

from sqlalchemy import Engine, create_engine
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from church_roster_bot.core.config import Settings, settings


class Base(DeclarativeBase):
    pass


def database_engine(config: Settings, *, unpooled: bool = False) -> Engine:
    """Bounded PostgreSQL connections; SQLite retains its simulation semantics."""
    from sqlalchemy.pool import NullPool

    url = make_url(config.database_url)
    options = {"pool_pre_ping": True, "hide_parameters": True}
    if unpooled:
        options["poolclass"] = NullPool
    if url.get_backend_name() == "postgresql":
        native = str(url.query.get("options", ""))
        options.update(
            isolation_level="READ COMMITTED",
            connect_args={
                "connect_timeout": config.db_connect_timeout_seconds,
                "options": native + " -c timezone=UTC"
                f" -c statement_timeout={config.db_statement_timeout_ms}"
                f" -c lock_timeout={config.db_lock_timeout_ms}"
                f" -c idle_in_transaction_session_timeout={config.db_idle_transaction_timeout_ms}",
            },
        )
        if not unpooled:
            options.update(
                pool_size=config.db_pool_size,
                max_overflow=config.db_max_overflow,
                pool_timeout=config.db_pool_timeout_seconds,
                pool_recycle=config.db_pool_recycle_seconds,
                pool_use_lifo=True,
            )
    return create_engine(url, **options)


engine = database_engine(settings)
SessionLocal = sessionmaker(engine, expire_on_commit=False)
logger = logging.getLogger(__name__)


def is_sqlite_busy(exc: BaseException) -> bool:
    return (
        isinstance(exc, OperationalError)
        and isinstance(exc.orig, sqlite3.OperationalError)
        and (getattr(exc.orig, "sqlite_errorcode", 0) & 0xFF)
        in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}
    )


def retry_sqlite_transaction[T](operation: Callable[[], T]) -> T:
    """Retry only a rolled-back database operation, never an external send."""
    while True:
        try:
            return operation()
        except OperationalError as exc:
            if not is_sqlite_busy(exc):
                raise
            logger.info("sqlite_busy_retry", extra={"operation": operation.__name__})
            time.sleep(0.2)


def get_session() -> Iterator[Session]:
    with SessionLocal() as session:
        yield session
