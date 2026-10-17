BEGIN;

-- Track temporary login-failure windows by hashed identifier.
-- "IP" limits and "EMAIL_IP" limits use separate rows.
CREATE TABLE login_rate_limits (
    scope VARCHAR(20) NOT NULL
        CHECK (scope IN ('IP', 'EMAIL_IP')),
    key_hash CHAR(64) NOT NULL,
    window_started_at TIMESTAMPTZ NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0
        CHECK (attempts >= 0),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (scope, key_hash)
);

CREATE INDEX idx_login_rate_limits_updated_at
    ON login_rate_limits (updated_at);

COMMIT;