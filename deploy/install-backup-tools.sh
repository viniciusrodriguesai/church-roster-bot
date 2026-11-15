#!/bin/bash
# Official standalone binaries, verified against signed release checksums.
# Does not configure accounts, start services, or overwrite installed binaries.
set -euo pipefail
umask 077
test "$(id -u)" = 0
test "$(uname -m)" = x86_64
for binary in restic rclone; do
    if command -v "$binary" >/dev/null; then
        echo "Existing $binary requires review." >&2
        exit 1
    fi
done
work=$(mktemp -d /var/tmp/roster-backup-tools.XXXXXXXX)
mkdir -m 700 "$work/gpg"
cd "$work"
fetch() { curl --fail --silent --show-error --location --proto '=https' --proto-redir '=https' "$1" -o "$2"; }
fetch https://restic.net/gpg-key-alex.asc restic-key.asc
fetch https://rclone.org/KEYS rclone-keys.asc
gpg --homedir "$work/gpg" --batch --import restic-key.asc rclone-keys.asc
gpg --homedir "$work/gpg" --batch --with-colons --fingerprint > fingerprints
grep -q 'CF8F18F2844575973F79D4E191A6868BD3F7A907' fingerprints
grep -q 'E3B358DC858FB307F48170B9CB0DBEBC5F32C81D' fingerprints
gpg --homedir "$work/gpg" --batch --export CF8F18F2844575973F79D4E191A6868BD3F7A907 > restic-keyring.gpg
gpg --homedir "$work/gpg" --batch --export E3B358DC858FB307F48170B9CB0DBEBC5F32C81D FBF737ECE9F8AB18604BD2AC93935E02FF3B54FA > rclone-keyring.gpg
restic_base=https://github.com/restic/restic/releases/download/v0.19.1
fetch "$restic_base/SHA256SUMS" restic-SHA256SUMS
fetch "$restic_base/SHA256SUMS.asc" restic-SHA256SUMS.asc
fetch "$restic_base/restic_0.19.1_linux_amd64.bz2" restic_0.19.1_linux_amd64.bz2
gpgv --keyring "$work/restic-keyring.gpg" restic-SHA256SUMS.asc restic-SHA256SUMS
awk '$2=="restic_0.19.1_linux_amd64.bz2"' restic-SHA256SUMS > restic-check
test "$(wc -l < restic-check)" = 1
sha256sum --check restic-check
rclone_base=https://downloads.rclone.org/v1.76.0
fetch "$rclone_base/SHA256SUMS" rclone-SHA256SUMS
fetch "$rclone_base/rclone-v1.76.0-linux-amd64.zip" rclone-v1.76.0-linux-amd64.zip
gpgv --keyring "$work/rclone-keyring.gpg" rclone-SHA256SUMS
awk '$2=="rclone-v1.76.0-linux-amd64.zip"' rclone-SHA256SUMS > rclone-check
test "$(wc -l < rclone-check)" = 1
sha256sum --check rclone-check
python3 - <<'PY'
import bz2
import os
from pathlib import Path
import zipfile

with bz2.open('restic_0.19.1_linux_amd64.bz2', 'rb') as source:
    with Path('/usr/local/bin/restic').open('xb') as target:
        target.write(source.read())
with zipfile.ZipFile('rclone-v1.76.0-linux-amd64.zip') as source:
    with Path('/usr/local/bin/rclone').open('xb') as target:
        target.write(source.read('rclone-v1.76.0-linux-amd64/rclone'))
for name in ('restic', 'rclone'):
    os.chmod('/usr/local/bin/' + name, 0o755)
PY
restic version
rclone version
printf 'Verified release downloads retained at %s\n' "$work"
