#!/bin/bash
# Root systemd job. Requires an initialized, approved EXTERNAL restic repository.
set -euo pipefail
umask 077
cd /opt/church-roster-bot
exec 9>/run/lock/church-roster-backup.lock
flock -n 9 || exit 1
source /etc/church-roster/backup.env
: "${RESTIC_REPOSITORY:?External repository required}"
: "${RESTIC_PASSWORD_FILE:?Password file required}"
case "$RESTIC_REPOSITORY" in
    sftp:*|s3:*|b2:*|rest:*|azure:*|gs:*|rclone:*) ;;
    *) echo 'Backup repository must be external.' >&2; exit 1 ;;
esac
export RESTIC_REPOSITORY RESTIC_PASSWORD_FILE
export RCLONE_CONFIG RESTIC_CACHE_DIR
# No local dump retention: bound staging to a single successful dump, leave failed
# staging intact for investigation; never automatically remove old files.
install -d -m 700 /var/lib/church-roster-backup
staging=$(mktemp -d /var/lib/church-roster-backup/run.XXXXXXXX)
df --output=avail -B1 /var/lib/church-roster-backup | tail -1 | \
    awk '$1 < 2147483648 {exit 1}' || { echo 'Less than 2 GiB free; backup aborted.' >&2; exit 1; }
docker compose --env-file /etc/church-roster/compose.env -f deploy/compose.production.yaml \
    exec -T -u postgres db pg_dump -U postgres -d roster --format=custom \
    --no-owner --no-privileges > "$staging/roster.dump"
docker compose --env-file /etc/church-roster/compose.env -f deploy/compose.production.yaml \
    exec -T -u postgres db pg_restore --list < "$staging/roster.dump" > /dev/null
sha256sum "$staging/roster.dump" > "$staging/roster.dump.sha256"
# Canonical stream path is stable between runs, so external retention groups correctly.
restic backup --stdin --stdin-filename roster.dump --tag church-roster-db < "$staging/roster.dump"
restic backup deploy/secrets deploy/state/tls /etc/church-roster/compose.env \
    --tag church-roster-config
restic check
# No forget/prune until the operator approves exact retention and recovery policy.
date -u +%FT%TZ > /var/lib/church-roster-backup/last-success
# Remove only files created by this invocation, after external verification.
rm -- "$staging/roster.dump" "$staging/roster.dump.sha256"
rmdir -- "$staging"
echo 'Encrypted external backup verified; restore drill is a separate required gate.'
