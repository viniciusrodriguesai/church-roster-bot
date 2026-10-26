"""Public health responses, real databases, deadlines and bounded probe resources."""

import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker

from church_roster_bot.app import create_app
from church_roster_bot.core.config import Settings


def settings(**overrides):
    return Settings(_env_file=None, **overrides)


@pytest.mark.parametrize(
    "roster", ["sqlite_file", pytest.param("postgres", marks=pytest.mark.postgres)], indirect=True
)
def test_health_with_real_database_preserves_application_pool(roster):
    factory = roster[1]
    app = create_app(factory, settings())
    with TestClient(app) as client:
        probe = app.state.readiness
        for _ in range(3):
            assert client.get("/health").json() == {"status": "ok"}
            assert client.get("/health/live").json() == {"status": "ok"}
            response = client.get("/health/ready")
            assert response.status_code == 200
            assert response.json() == {"status": "ok"}
            assert response.headers["cache-control"] == "no-store"
        if factory.kw["bind"].dialect.name == "postgresql":
            with factory() as db:
                assert (
                    db.scalar(
                        text(
                            "SELECT COUNT(*) FROM pg_stat_activity "
                            "WHERE application_name='church_roster_readiness'"
                        )
                    )
                    == 0
                )
    assert probe.closed
    with factory() as db:
        assert db.scalar(text("SELECT COUNT(*) FROM churches")) == 1


def test_missing_sqlite_database_is_not_created_by_readiness(tmp_path):
    path = tmp_path / "missing.sqlite3"
    engine = create_engine(f"sqlite+pysqlite:///{path}")
    try:
        with TestClient(create_app(sessionmaker(engine), settings())) as client:
            assert client.get("/health/ready").status_code == 503
            assert client.get("/health/live").status_code == 200
            assert client.get("/health").status_code == 200
        assert not path.exists()
    finally:
        engine.dispose()


def test_corrupted_sqlite_file_is_unready_without_modifying_it(tmp_path):
    path = tmp_path / "corrupt.sqlite3"
    content = b"intentionally invalid sqlite database for readiness"
    path.write_bytes(content)
    engine = create_engine(f"sqlite+pysqlite:///{path}")
    try:
        with TestClient(create_app(sessionmaker(engine), settings())) as client:
            assert client.get("/health/ready").status_code == 503
            assert client.get("/health/live").status_code == 200
        assert path.read_bytes() == content
    finally:
        engine.dispose()


def test_sqlite_path_with_spaces_and_unicode_is_probed_read_only(tmp_path):
    path = tmp_path / "health espaço.sqlite3"
    engine = create_engine(f"sqlite+pysqlite:///{path}")
    try:
        with engine.begin() as db:
            db.exec_driver_sql("CREATE TABLE readiness_fixture (id INTEGER)")
        before = path.read_bytes()
        with TestClient(create_app(sessionmaker(engine), settings())) as client:
            assert client.get("/health/ready").status_code == 200
        assert path.read_bytes() == before
    finally:
        engine.dispose()


@pytest.mark.postgres
@pytest.mark.parametrize("roster", ["postgres"], indirect=True)
def test_refused_postgresql_connection_is_unready_without_leaking_details(roster, caplog):
    with socket.socket() as occupied:
        occupied.bind(("127.0.0.1", 0))
        url = roster[1].kw["bind"].url.set(host="127.0.0.1", port=occupied.getsockname()[1])
        engine = create_engine(url)
        try:
            with TestClient(create_app(sessionmaker(engine), settings())) as client:
                response = client.get("/health/ready")
                assert response.status_code == 503
                assert response.json() == {"status": "unavailable"}
                assert client.get("/health/live").status_code == 200
                assert "connection refused" not in caplog.text.lower()
                assert not url.password or url.password not in caplog.text
        finally:
            engine.dispose()


def test_private_memory_database_is_not_shared_with_health_worker(roster):
    factory = roster[1]
    with factory.begin() as db:
        db.execute(text("UPDATE churches SET name='uncommitted health isolation check'"))
        with TestClient(create_app(factory, settings())) as client:
            assert client.get("/health/ready").status_code == 503
            assert client.get("/health/live").status_code == 200
        assert db.scalar(text("SELECT name FROM churches")) == "uncommitted health isolation check"
    with factory() as db:
        assert db.scalar(text("SELECT name FROM churches")) == "uncommitted health isolation check"


@pytest.mark.parametrize("roster", ["sqlite_file"], indirect=True)
def test_deadline_and_concurrent_checks_share_one_inflight_probe(roster, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    calls = 0
    app = create_app(roster[1], settings(health_ready_timeout_seconds=0.05))
    with TestClient(app) as client:
        probe = app.state.readiness

        def blocked():
            nonlocal calls
            calls += 1
            entered.set()
            assert release.wait(timeout=5)
            return True

        monkeypatch.setattr(probe, "_query", blocked)
        try:
            start = time.monotonic()
            with ThreadPoolExecutor(max_workers=8) as pool:
                responses = list(pool.map(lambda _: client.get("/health/ready"), range(8)))
            assert time.monotonic() - start < 2
            assert entered.is_set() and calls == 1
            assert all(
                r.status_code == 503 and r.json() == {"status": "unavailable"} for r in responses
            )
            assert client.get("/health/live").status_code == 200
        finally:
            release.set()
            probe.pending.result(timeout=5)
    assert probe.closed


@pytest.mark.parametrize("roster", ["sqlite_file"], indirect=True)
def test_dependency_errors_are_logged_without_details_and_recover(roster, monkeypatch, caplog):
    app = create_app(roster[1], settings())
    with TestClient(app) as client:
        probe = app.state.readiness
        original = probe.engine.connect

        def fail():
            raise RuntimeError("PRIVATE_PASSWORD_AND_DATABASE_HOST")

        monkeypatch.setattr(probe.engine, "connect", fail)
        response = client.get("/health/ready")
        assert response.status_code == 503
        assert response.json() == {"status": "unavailable"}
        assert "PRIVATE_PASSWORD" not in response.text + caplog.text
        assert "readiness_probe_failed" in caplog.text
        monkeypatch.setattr(probe.engine, "connect", original)
        assert client.get("/health/ready").status_code == 200


def test_readiness_requires_lifespan_but_legacy_health_remains_live(roster):
    client = TestClient(create_app(roster[1], settings()))
    assert client.get("/health/live").status_code == 200
    assert client.get("/health").status_code == 200
    assert client.get("/health/ready").status_code == 503
