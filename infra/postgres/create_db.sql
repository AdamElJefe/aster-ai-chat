-- Usage: psql -v dbname=aster -f create_db.sql   (as superuser, after init.sql)
SELECT format('CREATE DATABASE %I OWNER aster_owner', :'dbname')
 WHERE NOT EXISTS (SELECT 1 FROM pg_database WHERE datname = :'dbname') \gexec
\connect :dbname
ALTER SCHEMA public OWNER TO aster_owner;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO aster_app;
ALTER DEFAULT PRIVILEGES FOR ROLE aster_owner IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO aster_app;
ALTER DEFAULT PRIVILEGES FOR ROLE aster_owner IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO aster_app;
