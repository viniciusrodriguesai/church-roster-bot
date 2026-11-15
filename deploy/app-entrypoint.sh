#!/bin/sh
set -eu
# Read only the dedicated service credential; never echo it or pass it in argv.
DATABASE_URL=$(cat /run/secrets/database_url)
export DATABASE_URL
exec "$@"
