"""Three independent simulator processes share PostgreSQL after a real SQLite import."""

import json
import os
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.orm import sessionmaker

from alembic import command
from church_roster_bot.database.schema import migration_config
from church_roster_bot.database.source import file_hash
from church_roster_bot.database.transfer import transfer_sqlite
from church_roster_bot.scheduling.models import Assignment, ThirdDayRequest
from church_roster_bot.scripts.simulate import simulate
from church_roster_bot.whatsapp.models import InboundEvent, OutboundMessage
from tests.test_invitation_delivery import drain, prepare
from tests.test_phase2 import COORD, FIRST, SECOND, add


class ProcessTerminal:
    def __init__(self, phone, environment):
        self.events = []
        self.condition = threading.Condition()
        self.process = subprocess.Popen(
            [sys.executable, "-u", "-m", "church_roster_bot.scripts.simulate", "--phone", phone],
            env=environment,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            bufsize=1,
        )

        def collect(stream, kind):
            for line in stream:
                with self.condition:
                    self.events.append(
                        {
                            "utc": datetime.now(UTC).isoformat(),
                            "kind": kind,
                            "line": line.rstrip("\n"),
                        }
                    )
                    self.condition.notify_all()

        self.readers = [
            threading.Thread(target=collect, args=(stream, kind), daemon=True)
            for stream, kind in ((self.process.stdout, "output"), (self.process.stderr, "error"))
        ]
        for thread in self.readers:
            thread.start()

    def send(self, message):
        with self.condition:
            self.events.append(
                {"utc": datetime.now(UTC).isoformat(), "kind": "input", "line": message}
            )
        self.process.stdin.write(message + "\n")
        self.process.stdin.flush()

    def output(self):
        with self.condition:
            return "\n".join(e["line"] for e in self.events if e["kind"] == "output")

    def wait_for(self, text):
        with self.condition:
            assert self.condition.wait_for(lambda: text in self.output(), timeout=40), self.events

    def close(self):
        self.process.stdin.close()
        try:
            self.process.wait(timeout=15)
        except subprocess.TimeoutExpired:
            # This handle refers only to the subprocess created by this test.
            self.process.terminate()
            self.process.wait(timeout=10)
            raise
        for reader in self.readers:
            reader.join(5)
        assert self.process.returncode == 0, self.events
        assert not any(e["kind"] == "error" for e in self.events), self.events


@pytest.mark.postgres
@pytest.mark.parametrize("migration_url", ["postgres"], indirect=True)
@pytest.mark.parametrize("roster", ["sqlite_file"], indirect=True)
def test_three_real_processes_receive_once_after_migration(roster, migration_url, tmp_path):
    source_roster, source_factory, ids = roster
    member, slot, config = prepare(roster)
    add(source_roster, ids, SECOND)
    simulate(SECOND, "INICIAR", factory=source_factory, config=config)
    drain(source_factory, config)
    source = Path(source_factory.kw["bind"].url.database)
    original = file_hash(source), source.stat().st_mtime_ns
    evidence = Path(os.getenv("ROSTER_EVIDENCE_DIR", str(tmp_path)))
    evidence.mkdir(parents=True, exist_ok=True)
    target = create_engine(migration_url)
    terminals = {}
    restarted = None
    try:
        with target.begin() as db:
            cfg = migration_config()
            cfg.attributes["connection"] = db
            command.upgrade(cfg, "head")
            schema = db.scalar(text("SELECT current_schema()"))
        plan = transfer_sqlite(source, migration_url, schema=schema)
        imported = transfer_sqlite(
            source,
            migration_url,
            schema=schema,
            mode="apply",
            confirmation=plan["plan_token"],
            offline_confirmed=True,
        )
        factory = sessionmaker(target, expire_on_commit=False)
        environment = {
            **os.environ,
            "DATABASE_URL": migration_url,
            "APP_ENV": "test",
            "PYTHONUNBUFFERED": "1",
            "PYTHONIOENCODING": "utf-8",
        }
        for phone in (COORD, FIRST, SECOND):
            terminal = ProcessTerminal(phone, environment)
            terminals[phone] = terminal
            terminal.wait_for("Digite uma mensagem")
        assert len({t.process.pid for t in terminals.values()}) == 3
        terminals[COORD].send("SIM")
        terminals[FIRST].wait_for("Convite excepcional")
        with factory() as db:
            invitation_id = db.scalar(select(ThirdDayRequest.id))
        assert not any(e["kind"] == "input" for e in terminals[FIRST].events)
        terminals[FIRST].send(f"ACEITAR {invitation_id}")
        terminals[FIRST].wait_for("inscrição confirmada")
        terminals[COORD].wait_for("aceito.")
        assert [e["line"] for e in terminals[COORD].events if e["kind"] == "input"] == ["SIM"]
        assert "Convite excepcional" not in terminals[SECOND].output()
        assert "aceito." not in terminals[SECOND].output()
        with factory() as db:
            before = db.scalar(select(func.count(InboundEvent.id)))
        # Reproduce pasted terminal output, including legacy unmarked headings.
        copied = "\n".join(
            e["line"]
            for t in terminals.values()
            for e in t.events
            if e["kind"] == "output" and e["line"].startswith("[bot]")
        )
        terminals[FIRST].send(copied + "\n> MENU\nESCALAS DA IGREJA")
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline:
            assert all(t.process.poll() is None for t in terminals.values())
            time.sleep(0.05)
        with factory() as db:
            assert db.scalar(select(func.count(InboundEvent.id))) == before
        terminals[FIRST].send(f"ACEITAR {invitation_id}")
        terminals[FIRST].wait_for("already answered")
        with factory() as db:
            assert db.scalar(select(func.count(Assignment.id))) == 3
            exceptional = db.scalars(
                select(Assignment).where(Assignment.exception_id == invitation_id)
            ).one()
            assert (exceptional.volunteer_id, exceptional.slot_id, exceptional.status) == (
                member,
                slot,
                "confirmed",
            )
            notice = db.scalars(
                select(OutboundMessage).where(
                    OutboundMessage.dedupe_key == f"invitation:{invitation_id}:aceito"
                )
            ).one()
            assert (notice.status, notice.attempts) == ("sent", 1)
            assert all(
                n == 1 and status == "processed"
                for n, status in db.execute(select(InboundEvent.attempts, InboundEvent.status))
            )
            assert all(
                n == 1 and status == "sent"
                for n, status in db.execute(
                    select(OutboundMessage.attempts, OutboundMessage.status)
                )
            )
        assert terminals[COORD].output().count("aceito.") == 1
        assert terminals[FIRST].output().count("Convite excepcional") == 1
        assert all("ESCALAS DA IGREJA" not in t.output() for t in terminals.values())
        assert (file_hash(source), source.stat().st_mtime_ns) == original
        terminals[FIRST].close()
        restarted = ProcessTerminal(FIRST, environment)
        restarted.wait_for("Digite uma mensagem")
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            assert restarted.process.poll() is None
            assert "[bot]" not in restarted.output()
            time.sleep(0.05)
        (evidence / "simulator-migration.json").write_text(
            json.dumps(
                {
                    "status": "passed",
                    "table_proofs": imported["target_after"],
                    "source_sha256_unchanged": original[0],
                    "exceptional_assignments": 1,
                    "acceptance_notifications": 1,
                    "process_ids": {p: t.process.pid for p, t in terminals.items()},
                },
                indent=2,
            )
        )
    finally:
        cleanup_errors = []
        try:
            for terminal in terminals.values():
                if terminal.process.poll() is None:
                    try:
                        terminal.close()
                    except Exception as exc:
                        cleanup_errors.append(exc)
            if restarted is not None:
                try:
                    restarted.close()
                except Exception as exc:
                    cleanup_errors.append(exc)
        finally:
            try:
                for label, phone in (
                    ("coordinator", COORD),
                    ("volunteer-1", FIRST),
                    ("volunteer-2", SECOND),
                ):
                    if phone in terminals:
                        (evidence / f"transcript-{label}.json").write_text(
                            json.dumps(terminals[phone].events, ensure_ascii=True, indent=2)
                        )
                if restarted is not None:
                    (evidence / "transcript-volunteer-1-restart.json").write_text(
                        json.dumps(restarted.events, ensure_ascii=True, indent=2)
                    )
            finally:
                target.dispose()
        assert not cleanup_errors, cleanup_errors
        if SECOND in terminals:
            # Readers have joined: delayed output cannot escape the isolation check.
            assert "Convite excepcional" not in terminals[SECOND].output()
            assert "aceito." not in terminals[SECOND].output()
