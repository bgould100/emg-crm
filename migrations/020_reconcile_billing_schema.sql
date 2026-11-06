BEGIN;

-- =========================================================
-- 1. Contractor billing preferences
-- =========================================================

ALTER TABLE contractors
    ADD COLUMN IF NOT EXISTS billing_day INTEGER NOT NULL DEFAULT 28;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conrelid = 'contractors'::regclass
          AND conname = 'contractors_billing_day_check'
    ) THEN
        ALTER TABLE contractors
            ADD CONSTRAINT contractors_billing_day_check
            CHECK (billing_day BETWEEN 1 AND 28);
    END IF;
END
$$;


-- =========================================================
-- 2. Job-level collection freeze fields
-- =========================================================

ALTER TABLE jobs
    ADD COLUMN IF NOT EXISTS collections_frozen BOOLEAN NOT NULL DEFAULT FALSE,
    ADD COLUMN IF NOT EXISTS collections_frozen_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS collections_frozen_by VARCHAR(150),
    ADD COLUMN IF NOT EXISTS collections_freeze_reason TEXT;


-- =========================================================
-- 3. Homeowner installment records
-- =========================================================

CREATE TABLE IF NOT EXISTS job_installments (
    id BIGSERIAL PRIMARY KEY,

    agreement_id BIGINT NOT NULL
        REFERENCES job_payment_agreements(id),

    job_id BIGINT NOT NULL,
    contractor_id BIGINT NOT NULL,

    installment_number INTEGER NOT NULL
        CHECK (installment_number > 0),

    principal_amount NUMERIC(12,2) NOT NULL
        CHECK (principal_amount > 0),

    financing_interest NUMERIC(12,2) NOT NULL DEFAULT 0
        CHECK (financing_interest >= 0),

    due_date DATE NOT NULL,

    status VARCHAR(30) NOT NULL DEFAULT 'SCHEDULED'
        CHECK (
            status IN (
                'SCHEDULED',
                'ASSUMED_COLLECTED',
                'MISSED_REPORTED',
                'COLLECTED_CONFIRMED',
                'CANCELLED'
            )
        ),

    missed_reported_at TIMESTAMPTZ,
    missed_reported_by BIGINT,

    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,

    UNIQUE (agreement_id, installment_number),

    FOREIGN KEY (job_id, contractor_id)
        REFERENCES jobs (id, contractor_id)
);

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conrelid = 'job_installments'::regclass
          AND conname = 'job_installments_agreement_job_fkey'
    ) THEN
        ALTER TABLE job_installments
            ADD CONSTRAINT job_installments_agreement_job_fkey
            FOREIGN KEY (agreement_id, job_id)
            REFERENCES job_payment_agreements (id, job_id);
    END IF;
END
$$;

CREATE INDEX IF NOT EXISTS idx_job_installments_due
    ON job_installments (due_date, status);

CREATE INDEX IF NOT EXISTS idx_job_installments_job
    ON job_installments (job_id, due_date);


-- =========================================================
-- 4. Installment classification
-- =========================================================

ALTER TABLE job_installments
    ADD COLUMN IF NOT EXISTS installment_type VARCHAR(20)
        NOT NULL DEFAULT 'REGULAR';

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conrelid = 'job_installments'::regclass
          AND conname = 'job_installments_installment_type_check'
    ) THEN
        ALTER TABLE job_installments
            ADD CONSTRAINT job_installments_installment_type_check
            CHECK (
                installment_type IN (
                    'DOWN_PAYMENT',
                    'REGULAR',
                    'FINAL'
                )
            );
    END IF;
END
$$;


-- =========================================================
-- 5. Fee accrual source reconciliation
-- =========================================================

-- An accrual may originate from a collection or an
-- installment, but never both.
ALTER TABLE job_fee_accruals
    ALTER COLUMN collection_id DROP NOT NULL;

ALTER TABLE job_fee_accruals
    ADD COLUMN IF NOT EXISTS installment_id BIGINT;

CREATE UNIQUE INDEX IF NOT EXISTS
    job_fee_accruals_installment_id_key
    ON job_fee_accruals (installment_id);

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conrelid = 'job_fee_accruals'::regclass
          AND conname = 'job_fee_accruals_installment_id_fkey'
    ) THEN
        ALTER TABLE job_fee_accruals
            ADD CONSTRAINT job_fee_accruals_installment_id_fkey
            FOREIGN KEY (installment_id)
            REFERENCES job_installments(id);
    END IF;
END
$$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint
        WHERE conrelid = 'job_fee_accruals'::regclass
          AND conname = 'chk_fee_accrual_source'
    ) THEN
        ALTER TABLE job_fee_accruals
            ADD CONSTRAINT chk_fee_accrual_source
            CHECK (
                (
                    collection_id IS NOT NULL
                    AND installment_id IS NULL
                )
                OR
                (
                    collection_id IS NULL
                    AND installment_id IS NOT NULL
                )
            );
    END IF;
END
$$;


-- =========================================================
-- 6. Normalize the referral fee-rate constraint
-- =========================================================

-- This also repairs local databases where migration 014
-- was not previously applied.
ALTER TABLE job_fee_accruals
    DROP CONSTRAINT IF EXISTS job_fee_accruals_fee_rate_check;

ALTER TABLE job_fee_accruals
    ADD CONSTRAINT job_fee_accruals_fee_rate_check
    CHECK (fee_rate >= 0 AND fee_rate <= 1);


COMMIT;
