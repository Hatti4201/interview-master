BEGIN;

CREATE TABLE IF NOT EXISTS app_users (
    id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    email         TEXT NOT NULL CHECK (BTRIM(email) <> ''),
    display_name  TEXT,
    role          TEXT NOT NULL CHECK (role IN ('admin', 'user')),
    password_salt TEXT NOT NULL,
    password_hash CHAR(64) NOT NULL,
    is_active     BOOLEAN NOT NULL DEFAULT TRUE,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE UNIQUE INDEX IF NOT EXISTS app_users_email_unique
    ON app_users (LOWER(BTRIM(email)));

CREATE TABLE IF NOT EXISTS app_sessions (
    token_hash CHAR(64) PRIMARY KEY,
    user_id    BIGINT NOT NULL REFERENCES app_users(id) ON DELETE CASCADE,
    csrf_token CHAR(64) NOT NULL,
    expires_at TIMESTAMPTZ NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS app_sessions_user_idx ON app_sessions (user_id);
CREATE INDEX IF NOT EXISTS app_sessions_expiry_idx ON app_sessions (expires_at);

ALTER TABLE interviews
    ADD COLUMN IF NOT EXISTS created_by_user_id BIGINT REFERENCES app_users(id);

COMMIT;
