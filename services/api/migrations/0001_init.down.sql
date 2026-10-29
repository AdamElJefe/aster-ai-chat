DROP FUNCTION IF EXISTS claim_generation_job(integer);
DROP TABLE IF EXISTS audit_events, credit_ledger, idempotency_keys, generation_jobs CASCADE;
ALTER TABLE IF EXISTS message_turns DROP CONSTRAINT IF EXISTS message_turns_active_variant_fk;
DROP TABLE IF EXISTS messages, message_turns, conversations CASCADE;
DROP TABLE IF EXISTS character_versions, character_definitions CASCADE;
DROP TABLE IF EXISTS age_verification_sessions, age_attestations, sessions, identities, users, plan_definitions CASCADE;
DROP FUNCTION IF EXISTS credit_ledger_append_only(), character_versions_immutable(), app_user_id(), app_is_admin();
