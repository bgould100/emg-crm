BEGIN;

ALTER TABLE login_rate_limits
    ADD COLUMN in_flight INTEGER NOT NULL DEFAULT 0
        CHECK (in_flight >= 0);

COMMIT;