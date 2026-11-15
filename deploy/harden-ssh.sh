#!/bin/bash
# CRITICAL: run only after explicit approval, two live sessions and console recovery.
set -euo pipefail
umask 077
test "$(id -u)" = 0
test "${SSH_HARDEN_APPROVED:-}" = yes || { echo 'Explicit approval required.' >&2; exit 1; }
test -n "${SSH_CONNECTION:-}" || { echo 'Keep a live SSH recovery session.' >&2; exit 1; }
target=/etc/ssh/sshd_config.d/00-church-roster.conf
test ! -e "$target" || { echo 'Existing override requires manual review.' >&2; exit 1; }
/usr/sbin/sshd -t
work=$(mktemp -d /var/lib/roster-ssh-change.XXXXXXXX)
cp -a /etc/ssh/sshd_config "$work/sshd_config.before"
cp -a /etc/ssh/sshd_config.d "$work/sshd_config.d.before"
cat > "$work/rollback.sh" <<'ROLLBACK'
#!/bin/sh
set -eu
# Only remove the one new file, never host keys or preexisting configuration.
rm -f -- /etc/ssh/sshd_config.d/00-church-roster.conf
/usr/sbin/sshd -t
systemctl reload ssh.service
ROLLBACK
chmod 700 "$work/rollback.sh"
unit=roster-ssh-rollback-$(date +%s)
systemd-run --unit="$unit" --on-active=5m /bin/sh "$work/rollback.sh"
printf '%s\n' "$unit" > "$work/rollback-unit"
install -m 600 "$(dirname "$0")/ssh-hardening.conf" "$target"
client_ip=${SSH_CONNECTION%% *}
if ! /usr/sbin/sshd -t || \
   ! /usr/sbin/sshd -T -C "user=root,addr=$client_ip,host=$client_ip" | grep -qx 'permitrootlogin no'; then
    /bin/sh "$work/rollback.sh"
    exit 1
fi
/usr/sbin/sshd -T -C "user=rosteradmin,addr=$client_ip,host=$client_ip" | \
    grep -E '^(permitrootlogin|pubkeyauthentication|passwordauthentication|kbdinteractiveauthentication|maxauthtries|clientaliveinterval|clientalivecountmax) '
systemctl reload ssh.service
printf 'SSH reloaded; automatic rollback in 5 minutes. Keep this session open.\nRecovery unit: %s.timer\nRecovery directory: %s\n' "$unit" "$work"
# The operator must prove NEW rosteradmin SSH + sudo and root rejection before
# stopping the named timer. No automatic cancellation based only on reload success.
