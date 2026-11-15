#!/bin/sh
# Run as root ONLY after reviewing the target and approving deployment preparation.
set -eu
umask 077
cd "$(dirname "$0")"
[ "$(id -u)" = 0 ] || { echo 'Run with sudo.' >&2; exit 1; }
# Refuse existing material, including partial runs. Never rotate credentials implicitly.
[ ! -e secrets ] && [ ! -e state ] || { echo 'Existing state: manual review required.' >&2; exit 1; }
mkdir -m 700 secrets state
mkdir -m 700 state/tls
for role in postgres migrator runtime; do
    openssl rand -hex 32 > "secrets/${role}_password"
done
for role in migrator runtime; do
    password=$(cat "secrets/${role}_password")
    printf 'postgresql+psycopg://roster_%s:%s@db:5432/roster?sslmode=verify-full&sslrootcert=/run/roster/ca.crt\n' "$role" "$password" > "secrets/${role}_database_url"
done
unset password
# Secret parent stays root-only; files are mounted individually by rootful Compose.
chmod 444 secrets/*
openssl req -x509 -newkey rsa:3072 -nodes -days 3650 \
    -subj '/CN=Church Roster private database CA' \
    -keyout state/tls/ca.key -out state/tls/ca.crt 2>/dev/null
openssl req -new -newkey rsa:3072 -nodes -subj '/CN=db' \
    -keyout state/tls/server.key -out state/tls/server.csr 2>/dev/null
printf 'subjectAltName=DNS:db\nextendedKeyUsage=serverAuth\n' > state/tls/server.ext
openssl x509 -req -in state/tls/server.csr -CA state/tls/ca.crt \
    -CAkey state/tls/ca.key -CAcreateserial -days 397 \
    -extfile state/tls/server.ext -out state/tls/server.crt 2>/dev/null
chown root:999 state/tls/server.key
chmod 640 state/tls/server.key
chmod 444 state/tls/ca.crt state/tls/server.crt
openssl verify -CAfile state/tls/ca.crt -verify_hostname db state/tls/server.crt
echo 'Prepared new credentials and private database TLS. No services started.'
