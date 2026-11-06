
-- Distinguish down payments, regular installments, and final installments.
-- Existing installment records remain classified as REGULAR.

BEGIN;

ALTER TABLE job_installments
    ADD COLUMN installment_type VARCHAR(20) NOT NULL
        DEFAULT 'REGULAR';

ALTER TABLE job_installments
    ADD CONSTRAINT job_installments_installment_type_check
    CHECK (
        installment_type IN (
            'DOWN_PAYMENT',
            'REGULAR',
            'FINAL'
        )
    );

COMMIT;
