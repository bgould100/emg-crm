BEGIN;

-- Releasing a bid slot must not change the opportunity's status
-- or delete its associated job or history.
ALTER TABLE opportunities
    ADD COLUMN slot_released BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN slot_released_at TIMESTAMP WITHOUT TIME ZONE,
    ADD COLUMN slot_released_reason TEXT,
    ADD COLUMN slot_released_by VARCHAR(255);

-- A release must have complete audit information.
ALTER TABLE opportunities
    ADD CONSTRAINT opportunities_slot_release_audit_check
    CHECK (
        (
            slot_released = FALSE
            AND slot_released_at IS NULL
            AND slot_released_reason IS NULL
            AND slot_released_by IS NULL
        )
        OR
        (
            slot_released = TRUE
            AND slot_released_at IS NOT NULL
            AND slot_released_reason IS NOT NULL
            AND LENGTH(BTRIM(slot_released_reason)) > 0
            AND slot_released_by IS NOT NULL
            AND LENGTH(BTRIM(slot_released_by)) > 0
        )
    );

COMMIT;
