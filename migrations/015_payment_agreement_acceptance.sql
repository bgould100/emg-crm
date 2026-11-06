-- Add contractor acceptance lifecycle to payment agreements.
-- Only one accepted agreement may exist per job at a time.

BEGIN;

ALTER TABLE job_payment_agreements
    ADD COLUMN status VARCHAR(30) NOT NULL
        DEFAULT 'PENDING_ACCEPTANCE',
    ADD COLUMN accepted_at TIMESTAMPTZ,
    ADD COLUMN accepted_by_account_id BIGINT;

ALTER TABLE job_payment_agreements
    ADD CONSTRAINT job_payment_agreements_status_check
    CHECK (
        status IN (
            'PENDING_ACCEPTANCE',
            'ACCEPTED',
            'SUPERSEDED'
        )
    );

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
            status <> 'ACCEPTED'
            AND accepted_at IS NULL
            AND accepted_by_account_id IS NULL
        )
    );

ALTER TABLE job_payment_agreements
    ADD CONSTRAINT job_payment_agreements_accepted_by_fkey
    FOREIGN KEY (accepted_by_account_id, contractor_id)
    REFERENCES contractor_accounts (id, contractor_id);

CREATE UNIQUE INDEX idx_job_payment_agreements_one_accepted_per_job
    ON job_payment_agreements (job_id)
    WHERE status = 'ACCEPTED';

COMMIT;