"""Read-only readiness with one in-flight probe and an HTTP response deadline."""

import asyncio
import logging
import math
import threading
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from urllib.parse import quote

from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import URL
from sqlalchemy.pool import NullPool

logger = logging.getLogger(__name__)


class DatabaseReadiness:
    def __init__(self, url: URL, timeout: float):
        self.timeout = timeout
        self.closed = False
        self.pending: Future[bool] | None = None
        self._lock = threading.Lock()
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="readiness")
        self.engine: Engine | None = None
        backend = url.get_backend_name()
        if backend == "postgresql":
            milliseconds = max(1, math.ceil(timeout * 1000))
            options = str(url.query.get("options", ""))
            self.engine = create_engine(
                url,
                poolclass=NullPool,
                hide_parameters=True,
                connect_args={
                    "connect_timeout": max(2, math.ceil(timeout)),
                    "application_name": "church_roster_readiness",
                    "options": options + f" -c statement_timeout={milliseconds}"
                    f" -c lock_timeout={milliseconds} -c default_transaction_read_only=on",
                },
            )
        elif backend == "sqlite":
            database = url.database
            if not database or ":memory:" in database or url.query.get("mode") == "memory":
                # A private memory DB cannot be independently probed. Borrowing
                # its StaticPool connection could roll back another transaction.
                return
            if not database.startswith("file:"):
                database = "file:" + quote(Path(database).resolve().as_posix(), safe="/:")
            readonly = url.set(database=database).update_query_dict({"mode": "ro", "uri": "true"})
            self.engine = create_engine(
                readonly,
                poolclass=NullPool,
                hide_parameters=True,
                connect_args={"timeout": timeout},
            )

    def _query(self) -> bool:
        if self.engine is None:
            return False
        try:
            with self.engine.connect() as connection:
                if self.engine.dialect.name == "sqlite":
                    connection.exec_driver_sql("PRAGMA schema_version").scalar_one()
                return connection.scalar(text("SELECT 1")) == 1
        except Exception as exc:
            # Health responses and logs must not include driver messages, URLs,
            # credentials or SQL parameters, including for unexpected failures.
            logger.error("readiness_probe_failed", extra={"error_type": type(exc).__name__})
            return False

    async def check(self) -> bool:
        with self._lock:
            if self.closed or self.engine is None:
                return False
            # A timeout does not stop a synchronous DB driver. Share that exact
            # future until it finishes, rather than queueing abandoned probes.
            if self.pending is None or self.pending.done():
                self.pending = self._executor.submit(self._query)
            pending = self.pending
        try:
            result = asyncio.wrap_future(pending)
            return await asyncio.wait_for(asyncio.shield(result), timeout=self.timeout)
        except TimeoutError:
            logger.warning("readiness_probe_timed_out")
            return False
        except asyncio.CancelledError:
            if self.closed:
                return False
            raise

    def close(self) -> None:
        with self._lock:
            self.closed = True
        self._executor.shutdown(wait=False, cancel_futures=True)
        if self.engine is not None:
            self.engine.dispose()
