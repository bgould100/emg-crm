-- Migration 018: Use an accurate timestamp for fee accrual processing.

BEGIN;

ALTER TABLE job_fee_accruals
    ALTER COLUMN accrued_at SET DEFAULT clock_timestamp();

COMMIT;
