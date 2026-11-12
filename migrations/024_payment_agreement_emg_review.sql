BEGIN;

-- =========================================================
-- 1. Add the EMG custom-terms review state
-- =========================================================

ALTER TABLE job_payment_agreements
    DROP CONSTRAINT job_payment_agreements_status_check;

ALTER TABLE job_payment_agreements
    ADD CONSTRAINT job_payment_agreements_status_check
    CHECK (
        status IN (
            'PENDING_ACCEPTANCE',
            'PENDING_EMG_REVIEW',
            'ACCEPTED',
            'SUPERSEDED',
            'ADMIN_REVISED'
        )
    );


-- =========================================================
-- 2. Preserve acceptance-field integrity
-- =========================================================

ALTER TABLE job_payment_agreements
    DROP CONSTRAINT job_payment_agreements_acceptance_fields_check;

ALTER TABLE job_payment_agreements
    ADD CONSTRAINT job_payment_agreements_acceptance_fields_check
    CHECK (
        (
            status = 'ACCEPTED'
            AND accepted_at IS NOT NULL
            AND accepted_by_account_id IS NOT NULL
        )
        OR
        (
            status = 'SUPERSEDED'
            AND (
                (
                    accepted_at IS NULL
                    AND accepted_by_account_id IS NULL
                )
                OR
                (
                    accepted_at IS NOT NULL
                    AND accepted_by_account_id IS NOT NULL
                )
            )
        )
        OR
        (
            status IN (
                'PENDING_ACCEPTANCE',
                'PENDING_EMG_REVIEW',
                'ADMIN_REVISED'
            )
            AND accepted_at IS NULL
            AND accepted_by_account_id IS NULL
        )
    );


-- =========================================================
-- 3. Record contractor requests for custom EMG terms
-- =========================================================

ALTER TABLE job_payment_agreements
    ADD COLUMN emg_review_requested_at TIMESTAMPTZ,
    ADD COLUMN emg_review_request_note TEXT;

ALTER TABLE job_payment_agreements
    ADD CONSTRAINT job_payment_agreements_emg_review_fields_check
    CHECK (
        (
            status = 'PENDING_EMG_REVIEW'
            AND emg_review_requested_at IS NOT NULL
        )
        OR
        (
            status = 'SUPERSEDED'
            AND (
                (
                    emg_review_requested_at IS NULL
                    AND emg_review_request_note IS NULL
                )
                OR emg_review_requested_at IS NOT NULL
            )
        )
        OR
        (
            status IN (
                'PENDING_ACCEPTANCE',
                'ACCEPTED',
                'ADMIN_REVISED'
            )
            AND emg_review_requested_at IS NULL
            AND emg_review_request_note IS NULL
        )
    );


-- =========================================================
-- 4. Efficiently retrieve pending EMG review requests
-- =========================================================

CREATE INDEX idx_job_payment_agreements_pending_emg_review
    ON job_payment_agreements (
        emg_review_requested_at DESC,
        job_id
    )
    WHERE status = 'PENDING_EMG_REVIEW';

COMMIT;
