#!/bin/sh
# docker-entrypoint hook (runs after 01-init.sql created the roles): create the dev database
# with correct ownership and grants. Mirrors infra/postgres/create_db.sql.
set -eu
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" <<'SQL'
SELECT 'CREATE DATABASE aster OWNER aster_owner'
 WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = 'aster') \gexec
\connect aster
ALTER SCHEMA public OWNER TO aster_owner;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO aster_app;
ALTER DEFAULT PRIVILEGES FOR ROLE aster_owner IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO aster_app;
SQL
