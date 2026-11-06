-- Migration 023: Audit homeowner review invitation expiration

BEGIN;

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
            'EXPIRED'
        )
    );

COMMIT;
