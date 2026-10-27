BEGIN;

ALTER TABLE contractor_accounts
    ADD COLUMN IF NOT EXISTS must_change_password
    BOOLEAN NOT NULL DEFAULT FALSE;

COMMIT;
