"""Opt-in Linux Docker rehearsal of production services, with synthetic data only.

RUN_PRODUCTION_STACK_TEST=1 pytest -q tests/test_deployment_stack.py
Build church-roster-bot:phase43-validation first. All resources use a unique project.
"""

import json
import os
import re
import subprocess
from pathlib import Path
from uuid import uuid4

import pytest

ROOT = Path(__file__).resolve().parents[1]


def docker(*args, **kwargs):
    result = subprocess.run(
        ["docker", *args], capture_output=True, text=True, timeout=240, **kwargs
    )
    # Never print raw container output: it could contain generated credentials.
    if result.returncode != 0:
        # Diagnostics only for this isolated synthetic stack; redact connection URLs
        # and credential lines, and never emit stdout or environment/configuration.
        errors = []
        for line in result.stderr.splitlines()[-20:]:
            if any(word in line.lower() for word in ("password", "token", "secret")):
                continue
            errors.append(re.sub(r"postgresql[^\s]+", "[database URL redacted]", line))
        pytest.fail(f"Docker operation failed: {args[0]}\n" + "\n".join(errors))
    return result.stdout


@pytest.mark.skipif(
    os.getenv("RUN_PRODUCTION_STACK_TEST") != "1", reason="opt-in disposable Docker stack"
)
def test_tls_runtime_role_restart_and_https(tmp_path):
    project = "roster43test" + uuid4().hex[:12]
    fixture = project + "_fixture"
    env = {**os.environ, "RELEASE_TAG": "phase43-validation", "SITE_ADDRESS": "localhost"}
    config = json.loads(
        docker(
            "compose",
            "--profile",
            "maintenance",
            "-f",
            str(ROOT / "deploy/compose.production.yaml"),
            "config",
            "--format",
            "json",
            env=env,
        )
    )
    # Mirror production settings while storing generated Linux-permission fixtures
    # on an isolated volume, so the rehearsal works from Windows as well as Linux.
    config["name"] = project
    config.pop("secrets", None)
    config["volumes"]["fixture"] = {"external": True, "name": fixture}
    for name in ("postgres_data", "caddy_data", "caddy_config"):
        config["volumes"][name] = {"name": project + "_" + name}
    for name in config["networks"]:
        config["networks"][name]["name"] = project + "_" + name

    def mount(subpath, target):
        return {
            "type": "volume",
            "source": "fixture",
            "target": target,
            "read_only": True,
            "volume": {"subpath": subpath},
        }

    for name in ("api", "worker", "migrate"):
        service = config["services"][name]
        service.pop("secrets", None)
        service.pop("build", None)
        service["volumes"] = [
            mount("state/tls", "/run/roster"),
            mount("migrator" if name == "migrate" else "runtime", "/run/secrets"),
        ]
    config["services"]["worker"]["container_name"] = project + "_worker"
    db = config["services"]["db"]
    db.pop("secrets", None)
    db["volumes"] = [
        {"type": "volume", "source": "postgres_data", "target": "/var/lib/postgresql/data"},
        mount("state/tls", "/run/roster"),
        mount("secrets", "/run/secrets"),
        mount("init", "/docker-entrypoint-initdb.d"),
        mount("pgconf", "/etc/postgresql"),
    ]
    caddy = config["services"]["caddy"]
    caddy["ports"] = []
    caddy["volumes"] = [
        mount("caddy", "/etc/caddy"),
        {"type": "volume", "source": "caddy_data", "target": "/data"},
        {"type": "volume", "source": "caddy_config", "target": "/config"},
    ]
    path = tmp_path / "rehearsal.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    compose = ["compose", "-p", project, "-f", str(path)]
    docker("volume", "create", fixture)
    try:
        prepare = """
set -eu
cp /source/deploy/prepare-secrets.sh /fixture/prepare-secrets.sh
sh /fixture/prepare-secrets.sh
cd /fixture
mkdir runtime migrator init pgconf caddy
cp secrets/runtime_database_url runtime/database_url
cp secrets/migrator_database_url migrator/database_url
cp /source/deploy/postgres-init.sh init/10-roster.sh
cp /source/deploy/pg_hba.conf pgconf/pg_hba.conf
cp /source/deploy/Caddyfile caddy/Caddyfile
# Production mounts files separately (parent directories are not exposed).
# The fixture mounts subdirectories instead, which must be traversable by UID 999.
chmod 755 state/tls secrets
"""
        docker(
            "run",
            "--rm",
            "--entrypoint",
            "sh",
            "-v",
            fixture + ":/fixture",
            "-v",
            str(ROOT) + ":/source:ro",
            "postgres:17-bookworm",
            "-c",
            prepare,
        )
        docker(*compose, "up", "-d", "--wait", "db")
        docker(*compose, "--profile", "maintenance", "run", "--rm", "migrate")
        docker(
            *compose,
            "--profile",
            "maintenance",
            "run",
            "--rm",
            "migrate",
            "python",
            "-m",
            "church_roster_bot.scripts.seed_demo",
        )
        docker(*compose, "up", "-d", "--wait", "api", "worker", "caddy")
        check = """
from sqlalchemy import text
from church_roster_bot.core.database import engine
with engine.begin() as db:
    assert db.scalar(text('select current_user')) == 'roster_runtime'
    assert db.scalar(text('select ssl from pg_stat_ssl where pid=pg_backend_pid()')) is True
    assert db.scalar(text("select has_schema_privilege(current_user, 'public', 'CREATE')")) is False
    assert db.scalar(text('select count(*) from alembic_version')) == 1
    assert db.scalar(text('select count(*) from volunteers')) == 8
"""
        docker(
            *compose,
            "exec",
            "-T",
            "api",
            "/bin/sh",
            "/usr/local/bin/roster-entrypoint",
            "python",
            "-c",
            check,
        )
        docker(
            *compose, "exec", "-T", "caddy", "caddy", "validate", "--config", "/etc/caddy/Caddyfile"
        )
        caddy_id = docker(*compose, "ps", "-q", "caddy").strip()
        https_check = """
import json, ssl, time, urllib.request
for attempt in range(15):
    try:
        context = ssl.create_default_context(cafile='/data/caddy/pki/authorities/local/root.crt')
        address = 'https://localhost/health/ready'
        with urllib.request.urlopen(address, context=context, timeout=5) as response:
            assert json.load(response) == {'status': 'ok'}
        break
    except (OSError, urllib.error.URLError):
        if attempt == 14:
            raise
        time.sleep(1)
"""
        docker(
            "run",
            "--rm",
            "--read-only",
            "--user",
            "0",
            "--network",
            "container:" + caddy_id,
            "-v",
            project + "_caddy_data:/data:ro",
            "--entrypoint",
            "python",
            "church-roster-bot:phase43-validation",
            "-c",
            https_check,
        )
        restore = """
set -eu
pg_dump -U postgres -d roster --format=custom --no-owner --no-privileges --file=/tmp/rehearsal.dump
pg_restore --list /tmp/rehearsal.dump > /dev/null
createdb -U postgres rehearsal_restore
pg_restore -U postgres -d rehearsal_restore --single-transaction --exit-on-error \
    --no-owner --no-privileges /tmp/rehearsal.dump
test "$(psql -U postgres -d rehearsal_restore -Atc 'select count(*) from volunteers')" = 8
"""
        docker(*compose, "exec", "-T", "-u", "postgres", "db", "sh", "-c", restore)
        docker(*compose, "restart", "db", "api", "worker")
        docker(*compose, "up", "-d", "--wait", "api", "worker")
        docker(
            *compose,
            "exec",
            "-T",
            "api",
            "/bin/sh",
            "/usr/local/bin/roster-entrypoint",
            "python",
            "-c",
            check,
        )
    finally:
        # Only this test's random project, created here; no preexisting data touched.
        docker(*compose, "down", "--volumes")
        docker("volume", "rm", fixture)
