import logging
import sqlite3
import time
from collections.abc import Callable, Iterator

from sqlalchemy import create_engine
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from church_roster_bot.core.config import settings


class Base(DeclarativeBase):
    pass


engine = create_engine(settings.database_url, pool_pre_ping=True)
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
