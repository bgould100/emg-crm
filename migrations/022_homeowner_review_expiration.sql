-- Migration 022: Explicit homeowner review invitation expiration

BEGIN;

ALTER TABLE homeowner_review_invitations
    DROP CONSTRAINT homeowner_review_invitation_retry_state_check;

ALTER TABLE homeowner_review_invitations
    ADD CONSTRAINT homeowner_review_invitation_retry_state_check
    CHECK (
        retry_state IN (
            'ACTIVE',
            'PAUSED',
            'STOPPED',
            'COMPLETE',
            'EXPIRED'
        )
    );

UPDATE homeowner_review_invitations
SET
    retry_state = 'EXPIRED',
    next_attempt_at = NULL,
    claimed_until = NULL
WHERE expires_at <= CURRENT_TIMESTAMP
  AND submitted_at IS NULL
  AND retry_state IN ('ACTIVE', 'PAUSED');

COMMIT;
