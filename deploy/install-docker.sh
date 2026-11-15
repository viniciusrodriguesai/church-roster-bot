#!/bin/bash
# CRITICAL: package installation starts Docker and changes container networking.
# Use only after reviewing audit-host.sh output and obtaining explicit approval.
set -euo pipefail
umask 022
test "$(id -u)" = 0
test "${DOCKER_INSTALL_APPROVED:-}" = yes || { echo 'Explicit approval required.' >&2; exit 1; }
test "$(uname -m)" = x86_64
source /etc/os-release
test "$ID" = ubuntu && test "$VERSION_ID" = 24.04
for path in /var/lib/docker /etc/docker/daemon.json /etc/apt/sources.list.d/docker.sources /etc/apt/keyrings/docker.asc; do
    test ! -e "$path" || { echo "Existing $path: stop for manual review." >&2; exit 1; }
done
for package in docker.io docker-ce containerd containerd.io docker-compose docker-compose-v2 podman-docker; do
    if dpkg-query -W -f='${db:Status-Status}' "$package" 2>/dev/null | grep -qx installed; then
        echo "Existing package $package: stop for manual review." >&2
        exit 1
    fi
done
df --output=avail -B1 / | tail -1 | awk '$1 < 4294967296 {exit 1}'
apt-get update
apt-get install -y --no-remove ca-certificates curl gnupg
install -d -m 0755 /etc/apt/keyrings /etc/docker
curl --fail --silent --show-error --location --proto '=https' \
    https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
fingerprint=$(gpg --batch --show-keys --with-colons /etc/apt/keyrings/docker.asc | awk -F: '$1=="fpr" {print $10; exit}')
test "$fingerprint" = 9DC858229FC7DD38854AE2D88D81803C0EBFCD88 || \
    { echo 'Unexpected Docker repository signing key; installation stopped.' >&2; exit 1; }
cat > /etc/apt/sources.list.d/docker.sources <<'APT'
Types: deb
URIs: https://download.docker.com/linux/ubuntu
Suites: noble
Components: stable
Architectures: amd64
Signed-By: /etc/apt/keyrings/docker.asc
APT
cat > /etc/docker/daemon.json <<'JSON'
{
  "log-driver": "local",
  "log-opts": {"max-size": "10m", "max-file": "3"},
  "live-restore": true
}
JSON
apt-get update
apt-get install -y --no-remove docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
systemctl enable --now docker.service
docker version --format '{{.Server.Version}}'
docker compose version
systemctl is-active ssh.service docker.service
ufw status verbose
df -h /
echo 'Docker installed. No app, database, firewall opening or Docker group membership created.'
