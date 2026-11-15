#!/bin/bash
# Read-only checks. Failure is recorded by systemd; external alerts need a channel.
set -euo pipefail
cd /opt/church-roster-bot
compose=(docker compose --env-file /etc/church-roster/compose.env -f deploy/compose.production.yaml)
for service in db api worker caddy; do
    container=$("${compose[@]}" ps -q "$service")
    test -n "$container" || { echo "$service is missing" >&2; exit 1; }
    test "$(docker inspect --format '{{.State.Running}}' "$container")" = true || exit 1
    health=$(docker inspect --format '{{if .State.Health}}{{.State.Health.Status}}{{else}}none{{end}}' "$container")
    case "$health" in healthy|none) ;; *) echo "$service is unhealthy" >&2; exit 1 ;; esac
done
"${compose[@]}" exec -T api python -c \
    "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health/ready', timeout=4)" > /dev/null
df --output=avail -B1 / | tail -1 | awk '$1 < 4294967296 {exit 1}' || \
    { echo 'Disk warning: less than 4 GiB free.' >&2; exit 1; }
openssl x509 -checkend 2592000 -noout -in deploy/state/tls/server.crt || \
    { echo 'Database certificate expires within 30 days.' >&2; exit 1; }
test -f /var/lib/church-roster-backup/last-success || \
    { echo 'No verified external backup recorded.' >&2; exit 1; }
age=$(( $(date +%s) - $(stat -c %Y /var/lib/church-roster-backup/last-success) ))
test "$age" -lt 129600 || { echo 'External backup older than 36 hours.' >&2; exit 1; }
echo 'Services, database readiness, disk, certificate and backup age passed.'
