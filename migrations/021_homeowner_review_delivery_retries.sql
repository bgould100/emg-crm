
-- Migration 021: Homeowner review delivery retry controls and audit history

BEGIN;

-- --------------------------------------------------
-- Invitation retry controls
-- --------------------------------------------------

ALTER TABLE homeowner_review_invitations
    ADD COLUMN token_hash CHAR(64),
    ADD COLUMN retry_state VARCHAR(20) NOT NULL DEFAULT 'ACTIVE',
    ADD COLUMN failure_streak INTEGER NOT NULL DEFAULT 0,
    ADD COLUMN next_attempt_at TIMESTAMPTZ,
    ADD COLUMN last_attempt_at TIMESTAMPTZ,
    ADD COLUMN claimed_until TIMESTAMPTZ,
    ADD COLUMN paused_at TIMESTAMPTZ,
    ADD COLUMN stopped_at TIMESTAMPTZ,
    ADD COLUMN retry_control_admin_id BIGINT
        REFERENCES admin_users(id) ON DELETE SET NULL,
    ADD COLUMN retry_control_reason TEXT;

-- SHA-256 token hashes are 64 hexadecimal characters.
ALTER TABLE homeowner_review_invitations
    ADD CONSTRAINT homeowner_review_invitation_token_hash_check
        CHECK (
            token_hash IS NULL
            OR token_hash ~ '^[0-9a-f]{64}$'
        ),
    ADD CONSTRAINT homeowner_review_invitation_token_hash_unique
        UNIQUE (token_hash),
    ADD CONSTRAINT homeowner_review_invitation_retry_state_check
        CHECK (
            retry_state IN (
                'ACTIVE',
                'PAUSED',
                'STOPPED',
                'COMPLETE'
            )
        ),
    ADD CONSTRAINT homeowner_review_invitation_failure_streak_check
        CHECK (failure_streak >= 0),
    ADD CONSTRAINT homeowner_review_invitation_retry_state_consistency
        CHECK (
            (retry_state = 'PAUSED' AND paused_at IS NOT NULL)
            OR retry_state <> 'PAUSED'
        ),
    ADD CONSTRAINT homeowner_review_invitation_stopped_consistency
        CHECK (
            (retry_state = 'STOPPED' AND stopped_at IS NOT NULL)
            OR retry_state <> 'STOPPED'
        );

-- Existing invitations:
--   submitted, sent, and no-email invitations do not need another delivery.
--   pending/failed invitations remain eligible for delivery.
UPDATE homeowner_review_invitations
SET
    retry_state = CASE
        WHEN submitted_at IS NOT NULL
          OR delivery_status IN ('SENT', 'SKIPPED_NO_EMAIL')
            THEN 'COMPLETE'
        ELSE 'ACTIVE'
    END,
    next_attempt_at = CASE
        WHEN submitted_at IS NULL
          AND delivery_status IN ('PENDING', 'FAILED')
            THEN CURRENT_TIMESTAMP
        ELSE NULL
    END;

CREATE INDEX idx_homeowner_review_invitations_due
    ON homeowner_review_invitations(next_attempt_at, created_at)
    WHERE retry_state = 'ACTIVE'
      AND delivery_status IN ('PENDING', 'FAILED')
      AND submitted_at IS NULL;

CREATE INDEX idx_homeowner_review_invitations_expired_claims
    ON homeowner_review_invitations(claimed_until)
    WHERE claimed_until IS NOT NULL;

-- --------------------------------------------------
-- Delivery event history
-- --------------------------------------------------

CREATE TABLE homeowner_review_delivery_events (
    id BIGSERIAL PRIMARY KEY,

    invitation_id BIGINT NOT NULL
        REFERENCES homeowner_review_invitations(id)
        ON DELETE RESTRICT,

    event_type VARCHAR(30) NOT NULL
        CHECK (
            event_type IN (
                'ATTEMPT_STARTED',
                'ATTEMPT_SUCCEEDED',
                'ATTEMPT_FAILED',
                'PAUSED',
                'RESUMED',
                'STOPPED',
                'COMPLETED',
                'CLAIM_EXPIRED'
            )
        ),

    attempt_number INTEGER,
    failure_streak INTEGER,

    admin_user_id BIGINT
        REFERENCES admin_users(id)
        ON DELETE SET NULL,

    details JSONB NOT NULL DEFAULT '{}'::JSONB
        CHECK (jsonb_typeof(details) = 'object'),

    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT homeowner_review_delivery_event_attempt_check
        CHECK (attempt_number IS NULL OR attempt_number > 0),

    CONSTRAINT homeowner_review_delivery_event_streak_check
        CHECK (failure_streak IS NULL OR failure_streak >= 0)
);

CREATE INDEX idx_homeowner_review_delivery_events_invitation
    ON homeowner_review_delivery_events(invitation_id, created_at DESC);

CREATE INDEX idx_homeowner_review_delivery_events_admin
    ON homeowner_review_delivery_events(admin_user_id, created_at DESC)
    WHERE admin_user_id IS NOT NULL;

COMMIT;
