-- Contractor billing preferences: default to the 28th.
ALTER TABLE contractors
    ADD COLUMN IF NOT EXISTS billing_day INTEGER NOT NULL DEFAULT 28
    CHECK (billing_day BETWEEN 1 AND 28);

-- Job-level billing freeze. Freezing preserves all outstanding fees.
ALTER TABLE jobs
    ADD COLUMN IF NOT EXISTS collections_frozen BOOLEAN NOT NULL DEFAULT FALSE;

ALTER TABLE jobs
    ADD COLUMN IF NOT EXISTS collections_frozen_at TIMESTAMPTZ;

ALTER TABLE jobs
    ADD COLUMN IF NOT EXISTS collections_frozen_by VARCHAR(150);

ALTER TABLE jobs
    ADD COLUMN IF NOT EXISTS collections_freeze_reason TEXT;
-- Individual homeowner installments.
CREATE TABLE job_installments (
    id BIGSERIAL PRIMARY KEY,
    agreement_id BIGINT NOT NULL
        REFERENCES job_payment_agreements(id),
    job_id BIGINT NOT NULL,
    contractor_id BIGINT NOT NULL,
    installment_number INTEGER NOT NULL CHECK (installment_number > 0),
    principal_amount NUMERIC(12, 2) NOT NULL
        CHECK (principal_amount > 0),
    financing_interest NUMERIC(12, 2) NOT NULL DEFAULT 0
        CHECK (financing_interest >= 0),
    due_date DATE NOT NULL,
    status VARCHAR(30) NOT NULL DEFAULT 'SCHEDULED'
        CHECK (status IN (
            'SCHEDULED',
            'ASSUMED_COLLECTED',
            'MISSED_REPORTED',
            'COLLECTED_CONFIRMED',
            'CANCELLED'
        )),
    missed_reported_at TIMESTAMPTZ,
    missed_reported_by BIGINT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,

    UNIQUE (agreement_id, installment_number),
    FOREIGN KEY (job_id, contractor_id)
        REFERENCES jobs (id, contractor_id)
);

CREATE INDEX idx_job_installments_due
    ON job_installments (due_date, status);

CREATE INDEX idx_job_installments_job
    ON job_installments (job_id, due_date);
-- Allow fee accruals to originate from either a reported collection
-- or a scheduled installment, but never both.
ALTER TABLE job_fee_accruals
    ALTER COLUMN collection_id DROP NOT NULL;

ALTER TABLE job_fee_accruals
    ADD COLUMN installment_id BIGINT UNIQUE
    REFERENCES job_installments(id);

ALTER TABLE job_fee_accruals
    ADD CONSTRAINT chk_fee_accrual_source
    CHECK (
        (collection_id IS NOT NULL AND installment_id IS NULL)
        OR
        (collection_id IS NULL AND installment_id IS NOT NULL)
    );
-- One consolidated invoice per contractor per billing cycle.
CREATE TABLE contractor_invoices (
    id BIGSERIAL PRIMARY KEY,
    contractor_id BIGINT NOT NULL REFERENCES contractors(id),
    billing_period_start DATE NOT NULL,
    billing_period_end DATE NOT NULL,
    invoice_number VARCHAR(50) UNIQUE NOT NULL,
    status VARCHAR(20) NOT NULL DEFAULT 'DRAFT'
        CHECK (status IN ('DRAFT', 'SENT', 'PARTIALLY_PAID', 'PAID', 'VOID')),
    total_amount NUMERIC(12, 2) NOT NULL DEFAULT 0
        CHECK (total_amount >= 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    sent_at TIMESTAMPTZ,
    due_at TIMESTAMPTZ,
    UNIQUE (contractor_id, billing_period_start, billing_period_end),
    CHECK (billing_period_end >= billing_period_start)
);

-- Each fee accrual can appear on at most one invoice.
CREATE TABLE contractor_invoice_items (
    id BIGSERIAL PRIMARY KEY,
    invoice_id BIGINT NOT NULL REFERENCES contractor_invoices(id),
    fee_accrual_id BIGINT NOT NULL UNIQUE
        REFERENCES job_fee_accruals(id),
    amount NUMERIC(12, 2) NOT NULL CHECK (amount >= 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- Record every payment, including partial payments.
CREATE TABLE contractor_invoice_payments (
    id BIGSERIAL PRIMARY KEY,
    invoice_id BIGINT NOT NULL REFERENCES contractor_invoices(id),
    amount NUMERIC(12, 2) NOT NULL CHECK (amount > 0),
    received_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    payment_method VARCHAR(50),
    reference TEXT,
    recorded_by VARCHAR(150) NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_contractor_invoices_due
    ON contractor_invoices (due_at, status);

CREATE INDEX idx_contractor_invoice_payments_invoice
    ON contractor_invoice_payments (invoice_id, received_at);