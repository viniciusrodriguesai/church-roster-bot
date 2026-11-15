#!/bin/sh
# Read-only: no credentials, application environments, private keys or full logs.
set -u
printf '\n== Identity and OS ==\n'
id
uname -m
cat /etc/os-release
uptime
printf '\n== Capacity ==\n'
free -m
df -h / /var
df -i /
printf '\n== Listening sockets (no process arguments) ==\n'
ss -lntup
printf '\n== Noninteractive sudo ==\n'
sudo -n true || exit 1
printf '\n== Effective SSH policy for this connection ==\n'
client_ip=$(printf '%s' "$SSH_CONNECTION" | cut -d ' ' -f 1)
sudo -n /usr/sbin/sshd -T -C "user=rosteradmin,addr=$client_ip,host=$client_ip" | \
    grep -E '^(port|permitrootlogin|passwordauthentication|kbdinteractiveauthentication|pubkeyauthentication|authenticationmethods|maxauthtries|allowusers|clientaliveinterval|clientalivecountmax) '
printf '\n== SSH syntax and firewall ==\n'
sudo -n /usr/sbin/sshd -t
sudo -n ufw status verbose
printf '\n== Service states ==\n'
systemctl is-active ssh docker containerd unattended-upgrades fail2ban || true
systemctl is-enabled ssh docker unattended-upgrades || true
printf '\n== Docker inventory (no container environments) ==\n'
if command -v docker >/dev/null; then
    sudo -n docker version --format '{{.Server.Version}}'
    sudo -n docker ps -a --format '{{.Names}} {{.Image}} {{.Status}} {{.Ports}}'
    sudo -n docker volume ls
    sudo -n docker system df
fi
printf '\n== Existing deployment directory metadata ==\n'
for target in /opt/church-roster-bot /srv/church-roster-bot; do
    if [ -e "$target" ]; then sudo -n stat -c '%A %U:%G %n' "$target"; fi
done
printf '\n== Reboot requirement and failed services ==\n'
test ! -e /var/run/reboot-required || cat /var/run/reboot-required
systemctl --failed --no-pager
printf '\n== Time sync ==\n'
timedatectl show -p NTPSynchronized -p Timezone
printf '\n== Package inventory ==\n'
dpkg-query -W docker-ce docker.io containerd containerd.io docker-compose-plugin fail2ban unattended-upgrades 2>/dev/null || true
printf '\n== SSH socket activation ==\n'
systemctl is-enabled ssh.socket || true
printf '\n== Address families ==\n'
ip -brief address
printf '\n== Docker package candidates ==\n'
apt-cache policy docker.io docker-ce docker-compose-v2
printf '\n== Recovery tools ==\n'
command -v systemd-run
command -v openssl
printf '\n== Scheduled services ==\n'
systemctl list-timers --all --no-pager
