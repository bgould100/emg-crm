BEGIN;

-- One login account per contractor profile.
CREATE TABLE contractor_accounts (
    id BIGSERIAL PRIMARY KEY,
    contractor_id BIGINT NOT NULL UNIQUE
        REFERENCES contractors(id),
    email VARCHAR(255) NOT NULL,
    password_hash TEXT NOT NULL,
    status VARCHAR(20) NOT NULL DEFAULT 'ACTIVE'
        CHECK (status IN ('ACTIVE', 'DISABLED')),
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- Prevent duplicate login emails, regardless of capitalization.
CREATE UNIQUE INDEX idx_contractor_accounts_email_lower
    ON contractor_accounts (LOWER(email));

-- Store only a hash of each random session token, never the token itself.
CREATE TABLE contractor_sessions (
    id BIGSERIAL PRIMARY KEY,
    account_id BIGINT NOT NULL
        REFERENCES contractor_accounts(id),
    token_hash CHAR(64) NOT NULL UNIQUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    expires_at TIMESTAMPTZ NOT NULL,
    last_used_at TIMESTAMPTZ,
    revoked_at TIMESTAMPTZ
);

-- Support efficient lookup of active sessions by account.
CREATE INDEX idx_contractor_sessions_account_active
    ON contractor_sessions (account_id, expires_at)
    WHERE revoked_at IS NULL;

COMMIT;
