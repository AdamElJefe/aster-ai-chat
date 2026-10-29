-- 0001_init: identity, age gate, characters, conversations, generation jobs, ledger, audit.
-- Runs as the owner role. The application connects as aster_app (no BYPASSRLS, not owner).

CREATE TABLE plan_definitions (
    id                  text PRIMARY KEY,
    version             integer NOT NULL DEFAULT 1,
    display_name        text NOT NULL,
    daily_message_limit integer NOT NULL CHECK (daily_message_limit >= 0),
    max_message_chars   integer NOT NULL CHECK (max_message_chars > 0),
    created_at          timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE users (
    id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    handle     text NOT NULL UNIQUE CHECK (handle ~ '^[a-z0-9_]{3,32}$'),
    role       text NOT NULL DEFAULT 'user' CHECK (role IN ('user', 'admin')),
    status     text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'suspended')),
    plan_id    text NOT NULL DEFAULT 'dev_free' REFERENCES plan_definitions (id),
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE identities (
    id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id        uuid NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    provider       text NOT NULL,
    subject        text NOT NULL,
    email_hash     text,
    email_verified boolean NOT NULL DEFAULT false,
    created_at     timestamptz NOT NULL DEFAULT now(),
    UNIQUE (provider, subject)
);

-- Only token hashes are stored; the raw token lives in the HttpOnly cookie.
CREATE TABLE sessions (
    id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id    uuid NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    token_hash bytea NOT NULL UNIQUE,
    csrf_token text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz NOT NULL,
    revoked_at timestamptz
);
CREATE INDEX sessions_user_idx ON sessions (user_id);

-- Minimal attestation: never documents, DOB, or images.
CREATE TABLE age_attestations (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id         uuid NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    provider        text NOT NULL,
    attestation_ref text NOT NULL,
    age_verified    boolean NOT NULL,
    region          text NOT NULL,
    policy_version  text NOT NULL,
    verified_at     timestamptz NOT NULL DEFAULT now(),
    expires_at      timestamptz NOT NULL,
    revoked_at      timestamptz,
    UNIQUE (provider, attestation_ref)
);
CREATE INDEX age_attestations_user_idx ON age_attestations (user_id, expires_at DESC);

-- Single-use verification sessions (replay protection).
CREATE TABLE age_verification_sessions (
    id         uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id    uuid NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    provider   text NOT NULL,
    region     text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz NOT NULL,
    used_at    timestamptz
);

CREATE TABLE character_definitions (
    id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    creator_id       uuid NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    current_version  integer NOT NULL DEFAULT 1,
    visibility       text NOT NULL DEFAULT 'private'
                     CHECK (visibility IN ('private', 'unlisted', 'public')),
    moderation_state text NOT NULL DEFAULT 'draft'
                     CHECK (moderation_state IN ('draft', 'pending_review', 'approved', 'rejected')),
    is_curated       boolean NOT NULL DEFAULT false,
    tags             text[] NOT NULL DEFAULT '{}',
    created_at       timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz NOT NULL DEFAULT now(),
    -- Nothing becomes visible to others without approval.
    CONSTRAINT visibility_requires_approval
        CHECK (visibility = 'private' OR moderation_state = 'approved')
);
CREATE INDEX character_definitions_catalog_idx
    ON character_definitions (visibility, moderation_state, created_at DESC);
CREATE INDEX character_definitions_creator_idx ON character_definitions (creator_id, created_at DESC);
CREATE INDEX character_definitions_tags_idx ON character_definitions USING gin (tags);

CREATE TABLE character_versions (
    character_id  uuid NOT NULL REFERENCES character_definitions (id) ON DELETE CASCADE,
    version       integer NOT NULL CHECK (version >= 1),
    display_name  text NOT NULL CHECK (length(display_name) BETWEEN 1 AND 80),
    declared_age  integer NOT NULL CHECK (declared_age >= 18 AND declared_age <= 1000),
    card          jsonb NOT NULL,
    content_hash  text NOT NULL,
    created_at    timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (character_id, version)
);

-- Versions are immutable once written.
CREATE FUNCTION character_versions_immutable() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'character_versions rows are immutable';
END;
$$;
CREATE TRIGGER character_versions_no_update
    BEFORE UPDATE ON character_versions
    FOR EACH ROW EXECUTE FUNCTION character_versions_immutable();

CREATE TABLE conversations (
    id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id           uuid NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    character_id      uuid NOT NULL,
    character_version integer NOT NULL,
    title             text NOT NULL DEFAULT '' CHECK (length(title) <= 120),
    status            text NOT NULL DEFAULT 'active' CHECK (status IN ('active', 'archived')),
    config            jsonb NOT NULL,
    created_at        timestamptz NOT NULL DEFAULT now(),
    updated_at        timestamptz NOT NULL DEFAULT now(),
    last_message_at   timestamptz NOT NULL DEFAULT now(),
    FOREIGN KEY (character_id, character_version)
        REFERENCES character_versions (character_id, version) ON DELETE CASCADE
);
CREATE INDEX conversations_user_recent_idx ON conversations (user_id, last_message_at DESC, id DESC);

CREATE TABLE message_turns (
    id                uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    conversation_id   uuid NOT NULL REFERENCES conversations (id) ON DELETE CASCADE,
    user_id           uuid NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    ordinal           integer NOT NULL CHECK (ordinal >= 0),
    active_variant_id uuid,
    created_at        timestamptz NOT NULL DEFAULT now(),
    UNIQUE (conversation_id, ordinal)
);

CREATE TABLE messages (
    id                 uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    conversation_id    uuid NOT NULL REFERENCES conversations (id) ON DELETE CASCADE,
    turn_id            uuid NOT NULL REFERENCES message_turns (id) ON DELETE CASCADE,
    user_id            uuid NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    role               text NOT NULL CHECK (role IN ('user', 'assistant')),
    variant            integer NOT NULL DEFAULT 0 CHECK (variant >= 0),
    state              text NOT NULL CHECK (state IN ('pending', 'streaming', 'completed', 'failed', 'blocked')),
    content_ciphertext bytea,
    key_id             text,
    client_message_id  uuid,
    prompt_tokens      integer,
    completion_tokens  integer,
    model_id           text,
    template_version   text,
    finish_reason      text,
    created_at         timestamptz NOT NULL DEFAULT now(),
    completed_at       timestamptz,
    UNIQUE (turn_id, role, variant),
    CHECK (state <> 'completed' OR content_ciphertext IS NOT NULL)
);
CREATE INDEX messages_conversation_idx ON messages (conversation_id, created_at);

ALTER TABLE message_turns
    ADD CONSTRAINT message_turns_active_variant_fk
    FOREIGN KEY (active_variant_id) REFERENCES messages (id) ON DELETE SET NULL DEFERRABLE INITIALLY DEFERRED;

-- Outbox / work queue for the chat worker. id doubles as the public request_id.
CREATE TABLE generation_jobs (
    id                   uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id              uuid NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    conversation_id      uuid NOT NULL REFERENCES conversations (id) ON DELETE CASCADE,
    assistant_message_id uuid NOT NULL REFERENCES messages (id) ON DELETE CASCADE,
    kind                 text NOT NULL CHECK (kind IN ('reply', 'regenerate')),
    status               text NOT NULL DEFAULT 'queued'
                         CHECK (status IN ('queued', 'running', 'completed', 'failed', 'canceled')),
    attempt              integer NOT NULL DEFAULT 0,
    lease_expires_at     timestamptz,
    error_code           text,
    created_at           timestamptz NOT NULL DEFAULT now(),
    updated_at           timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX generation_jobs_claim_idx ON generation_jobs (status, created_at)
    WHERE status IN ('queued', 'running');
-- At most one in-flight generation per conversation.
CREATE UNIQUE INDEX generation_jobs_one_inflight_idx ON generation_jobs (conversation_id)
    WHERE status IN ('queued', 'running');

CREATE TABLE idempotency_keys (
    user_id      uuid NOT NULL REFERENCES users (id) ON DELETE CASCADE,
    purpose      text NOT NULL,
    key          text NOT NULL CHECK (length(key) BETWEEN 8 AND 128),
    request_hash text NOT NULL,
    response     jsonb NOT NULL,
    created_at   timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (user_id, purpose, key)
);

-- Append-only usage ledger. user_id is nulled on account deletion; subject_hash keeps
-- rows reconcilable without identifying the person.
CREATE TABLE credit_ledger (
    id              uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    user_id         uuid REFERENCES users (id) ON DELETE SET NULL,
    subject_hash    text NOT NULL,
    unit            text NOT NULL CHECK (unit IN ('chat_message')),
    event_type      text NOT NULL CHECK (event_type IN ('grant', 'reserve', 'settle', 'release', 'adjustment')),
    amount          integer NOT NULL,
    causation_id    uuid,
    idempotency_key text,
    metadata        jsonb NOT NULL DEFAULT '{}',
    created_at      timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX credit_ledger_user_idx ON credit_ledger (user_id, created_at DESC);
-- Each job can be reserved, settled, released at most once: idempotent settlement.
CREATE UNIQUE INDEX credit_ledger_once_per_cause_idx ON credit_ledger (causation_id, event_type)
    WHERE causation_id IS NOT NULL;

CREATE FUNCTION credit_ledger_append_only() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    -- Allow the FK's ON DELETE SET NULL on user_id; nothing else may change.
    IF TG_OP = 'UPDATE'
       AND NEW.user_id IS NULL AND OLD.user_id IS NOT NULL
       AND NEW.id = OLD.id AND NEW.amount = OLD.amount AND NEW.event_type = OLD.event_type
       AND NEW.subject_hash = OLD.subject_hash THEN
        RETURN NEW;
    END IF;
    RAISE EXCEPTION 'credit_ledger is append-only';
END;
$$;
CREATE TRIGGER credit_ledger_no_mutation
    BEFORE UPDATE OR DELETE ON credit_ledger
    FOR EACH ROW EXECUTE FUNCTION credit_ledger_append_only();

CREATE TABLE audit_events (
    id            uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    actor_user_id uuid,
    action        text NOT NULL,
    subject_ref   text NOT NULL,
    reason        text,
    ip_hash       text,
    attrs         jsonb NOT NULL DEFAULT '{}',
    created_at    timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX audit_events_subject_idx ON audit_events (subject_ref, created_at DESC);

-- ---------------------------------------------------------------------------
-- Row-level security. app.user_id / app.role are set per transaction with SET LOCAL.
-- ---------------------------------------------------------------------------
CREATE FUNCTION app_user_id() RETURNS uuid
LANGUAGE sql STABLE AS $$ SELECT nullif(current_setting('app.user_id', true), '')::uuid $$;

CREATE FUNCTION app_is_admin() RETURNS boolean
LANGUAGE sql STABLE AS $$ SELECT coalesce(current_setting('app.role', true), '') = 'admin' $$;

ALTER TABLE age_attestations ENABLE ROW LEVEL SECURITY;
CREATE POLICY owner_rw ON age_attestations
    USING (user_id = app_user_id()) WITH CHECK (user_id = app_user_id());

ALTER TABLE age_verification_sessions ENABLE ROW LEVEL SECURITY;
CREATE POLICY owner_rw ON age_verification_sessions
    USING (user_id = app_user_id()) WITH CHECK (user_id = app_user_id());

ALTER TABLE character_definitions ENABLE ROW LEVEL SECURITY;
CREATE POLICY read_visible ON character_definitions FOR SELECT
    USING (creator_id = app_user_id()
           OR (visibility IN ('public', 'unlisted') AND moderation_state = 'approved')
           OR app_is_admin());
CREATE POLICY owner_insert ON character_definitions FOR INSERT
    WITH CHECK (creator_id = app_user_id());
CREATE POLICY owner_or_admin_update ON character_definitions FOR UPDATE
    USING (creator_id = app_user_id() OR app_is_admin())
    WITH CHECK (creator_id = app_user_id() OR app_is_admin());
CREATE POLICY owner_delete ON character_definitions FOR DELETE
    USING (creator_id = app_user_id());

ALTER TABLE character_versions ENABLE ROW LEVEL SECURITY;
CREATE POLICY read_visible ON character_versions FOR SELECT
    USING (EXISTS (SELECT 1 FROM character_definitions d WHERE d.id = character_id));
CREATE POLICY owner_insert ON character_versions FOR INSERT
    WITH CHECK (EXISTS (SELECT 1 FROM character_definitions d
                        WHERE d.id = character_id AND d.creator_id = app_user_id()));

ALTER TABLE conversations ENABLE ROW LEVEL SECURITY;
CREATE POLICY owner_rw ON conversations
    USING (user_id = app_user_id()) WITH CHECK (user_id = app_user_id());

ALTER TABLE message_turns ENABLE ROW LEVEL SECURITY;
CREATE POLICY owner_rw ON message_turns
    USING (user_id = app_user_id()) WITH CHECK (user_id = app_user_id());

ALTER TABLE messages ENABLE ROW LEVEL SECURITY;
CREATE POLICY owner_rw ON messages
    USING (user_id = app_user_id()) WITH CHECK (user_id = app_user_id());

ALTER TABLE generation_jobs ENABLE ROW LEVEL SECURITY;
CREATE POLICY owner_rw ON generation_jobs
    USING (user_id = app_user_id()) WITH CHECK (user_id = app_user_id());

ALTER TABLE idempotency_keys ENABLE ROW LEVEL SECURITY;
CREATE POLICY owner_rw ON idempotency_keys
    USING (user_id = app_user_id()) WITH CHECK (user_id = app_user_id());

ALTER TABLE credit_ledger ENABLE ROW LEVEL SECURITY;
CREATE POLICY owner_read ON credit_ledger FOR SELECT USING (user_id = app_user_id());
CREATE POLICY owner_insert ON credit_ledger FOR INSERT WITH CHECK (user_id = app_user_id());

-- The app may write audit events but never read them back (admin tooling uses a separate role later).
ALTER TABLE audit_events ENABLE ROW LEVEL SECURITY;
CREATE POLICY insert_only ON audit_events FOR INSERT WITH CHECK (true);

-- Worker job claim: the only cross-tenant read the app role can perform, exposed as a
-- narrow SECURITY DEFINER function that returns (job id, owner) and nothing else.
CREATE FUNCTION claim_generation_job(lease_seconds integer)
RETURNS TABLE (job_id uuid, owner_id uuid, attempt integer)
LANGUAGE sql SECURITY DEFINER SET search_path = public AS $$
    UPDATE generation_jobs j
       SET status = 'running',
           attempt = j.attempt + 1,
           lease_expires_at = now() + make_interval(secs => lease_seconds),
           updated_at = now()
     WHERE j.id = (
           SELECT id FROM generation_jobs
            WHERE status = 'queued'
               OR (status = 'running' AND lease_expires_at < now())
            ORDER BY created_at
            FOR UPDATE SKIP LOCKED
            LIMIT 1)
    RETURNING j.id, j.user_id, j.attempt;
$$;
