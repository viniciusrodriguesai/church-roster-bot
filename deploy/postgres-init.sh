#!/bin/sh
set -eu
# Official entrypoint runs this once, and only for an empty volume.
MIGRATOR_PASSWORD=$(cat /run/secrets/migrator_password)
RUNTIME_PASSWORD=$(cat /run/secrets/runtime_password)
export MIGRATOR_PASSWORD RUNTIME_PASSWORD
psql -v ON_ERROR_STOP=1 --username postgres --dbname roster <<'SQL'
\getenv migrator_password MIGRATOR_PASSWORD
\getenv runtime_password RUNTIME_PASSWORD
CREATE ROLE roster_migrator LOGIN PASSWORD :'migrator_password' NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
CREATE ROLE roster_runtime LOGIN PASSWORD :'runtime_password' NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS;
REVOKE ALL ON DATABASE roster FROM PUBLIC;
GRANT CONNECT ON DATABASE roster TO roster_migrator, roster_runtime;
REVOKE ALL ON SCHEMA public FROM PUBLIC;
ALTER SCHEMA public OWNER TO roster_migrator;
GRANT USAGE ON SCHEMA public TO roster_runtime;
ALTER DEFAULT PRIVILEGES FOR ROLE roster_migrator IN SCHEMA public GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO roster_runtime;
ALTER DEFAULT PRIVILEGES FOR ROLE roster_migrator IN SCHEMA public GRANT USAGE, SELECT ON SEQUENCES TO roster_runtime;
SQL
unset MIGRATOR_PASSWORD RUNTIME_PASSWORD
