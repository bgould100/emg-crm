-- Permit zero-percent referral fee accruals.
-- Existing accrual records and their historical rates remain unchanged.

BEGIN;

ALTER TABLE job_fee_accruals
    DROP CONSTRAINT IF EXISTS job_fee_accruals_fee_rate_check;

ALTER TABLE job_fee_accruals
    ADD CONSTRAINT job_fee_accruals_fee_rate_check
    CHECK (fee_rate >= 0 AND fee_rate <= 1);

COMMIT;