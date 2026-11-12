-- Migration 028: Handle uncertain homeowner review email outcomes.
--
-- An expired worker claim or ambiguous SES result must not be retried
-- automatically. An administrator must explicitly resolve the outcome.
--
-- Intentionally does not backfill or modify existing invitations.

BEGIN;

-- Allow the delivery outcome to remain explicitly unknown.
ALTER TABLE homeowner_review_invitations
    DROP CONSTRAINT homeowner_review_invitations_delivery_status_check;

ALTER TABLE homeowner_review_invitations
    ADD CONSTRAINT homeowner_review_invitations_delivery_status_check
    CHECK (
        delivery_status IN (
            'PENDING',
            'SENT',
            'FAILED',
            'SKIPPED_NO_EMAIL',
            'UNKNOWN'
        )
    );

-- UNKNOWN means automatic delivery is suspended pending an admin decision.
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
            'EXPIRED',
            'UNKNOWN'
        )
    );

-- Keep the uncertain state internally consistent. COMPLETE is also
-- permitted with delivery_status UNKNOWN when an administrator chooses
-- not to resend because the original email may have been accepted.
ALTER TABLE homeowner_review_invitations
    ADD CONSTRAINT homeowner_review_invitation_unknown_state_check
    CHECK (
        (
            retry_state <> 'UNKNOWN'
            OR (
                delivery_status = 'UNKNOWN'
                AND next_attempt_at IS NULL
                AND claimed_until IS NULL
            )
        )
        AND (
            delivery_status <> 'UNKNOWN'
            OR retry_state IN ('UNKNOWN', 'COMPLETE')
        )
    );

-- Record ambiguous outcomes and explicit administrator resolutions.
ALTER TABLE homeowner_review_delivery_events
    DROP CONSTRAINT homeowner_review_delivery_events_event_type_check;

ALTER TABLE homeowner_review_delivery_events
    ADD CONSTRAINT homeowner_review_delivery_events_event_type_check
    CHECK (
        event_type IN (
            'ATTEMPT_STARTED',
            'ATTEMPT_SUCCEEDED',
            'ATTEMPT_FAILED',
            'PAUSED',
            'RESUMED',
            'STOPPED',
            'COMPLETED',
            'CLAIM_EXPIRED',
            'EXPIRED',
            'OUTCOME_UNKNOWN',
            'ADMIN_RESOLVED',
            'ADMIN_RESEND'
        )
    );

COMMIT;