-- Bootstraps roles and the dev database. Run once as a superuser (docker-entrypoint does this).
-- Passwords here are DEV ONLY; staging/prod roles are created by IaC with secrets from the vault.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'aster_owner') THEN
        CREATE ROLE aster_owner LOGIN PASSWORD 'aster_owner_dev';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'aster_app') THEN
        -- No BYPASSRLS, not an owner of anything: RLS always applies to this role.
        CREATE ROLE aster_app LOGIN PASSWORD 'aster_app_dev' NOSUPERUSER NOBYPASSRLS NOCREATEDB NOCREATEROLE;
    END IF;
END
$$;
