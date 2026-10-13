BEGIN;

-- Enforce that financial records belong to the contractor assigned to the job.
CREATE UNIQUE INDEX uq_jobs_id_contractor_id
    ON jobs (id, contractor_id);

CREATE UNIQUE INDEX uq_contractor_accounts_id_contractor_id
    ON contractor_accounts (id, contractor_id);

-- Each agreement version is retained so changes do not erase prior terms.
CREATE TABLE job_payment_agreements (
    id BIGSERIAL PRIMARY KEY,
    job_id BIGINT NOT NULL,
    contractor_id BIGINT NOT NULL,
    version INTEGER NOT NULL CHECK (version > 0),
    arrangement_type VARCHAR(20) NOT NULL
        CHECK (arrangement_type IN ('PAID_IN_FULL', 'PAYMENT_PLAN')),
    invoice_total NUMERIC(12, 2) NOT NULL
        CHECK (invoice_total > 0),
    schedule_type VARCHAR(20),
    installment_amount NUMERIC(12, 2),
    final_installment_amount NUMERIC(12, 2),
    plan_start_date DATE,
    final_due_date DATE,
    custom_schedule JSONB,
    paid_in_full_at TIMESTAMPTZ,
    created_by_account_id BIGINT NOT NULL,
    supersedes_agreement_id BIGINT REFERENCES job_payment_agreements(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,

    UNIQUE (job_id, version),
    FOREIGN KEY (job_id, contractor_id)
        REFERENCES jobs (id, contractor_id),
    FOREIGN KEY (created_by_account_id, contractor_id)
        REFERENCES contractor_accounts (id, contractor_id),

    CHECK (
        supersedes_agreement_id IS NULL
        OR supersedes_agreement_id <> id
    ),
    CHECK (
        (
            arrangement_type = 'PAID_IN_FULL'
            AND schedule_type IS NULL
            AND installment_amount IS NULL
            AND final_installment_amount IS NULL
            AND plan_start_date IS NULL
            AND final_due_date IS NULL
            AND custom_schedule IS NULL
        )
        OR
        (
            arrangement_type = 'PAYMENT_PLAN'
            AND schedule_type IN ('WEEKLY', 'BIWEEKLY', 'MONTHLY', 'CUSTOM')
            AND installment_amount > 0
            AND plan_start_date IS NOT NULL
            AND final_due_date IS NOT NULL
            AND final_due_date >= plan_start_date
            AND (
                (schedule_type = 'CUSTOM' AND custom_schedule IS NOT NULL)
                OR
                (schedule_type <> 'CUSTOM' AND custom_schedule IS NULL)
            )
            AND (
                final_installment_amount IS NULL
                OR final_installment_amount > 0
            )
        )
    )
);

CREATE INDEX idx_job_payment_agreements_job_version
    ON job_payment_agreements (job_id, version DESC);

-- Each report represents a contractor-reported collection.
-- Corrections should supersede an earlier record rather than erase it.
CREATE TABLE job_collections (
    id BIGSERIAL PRIMARY KEY,
    job_id BIGINT NOT NULL,
    contractor_id BIGINT NOT NULL,
    reported_by_account_id BIGINT NOT NULL,
    amount NUMERIC(12, 2) NOT NULL CHECK (amount > 0),
    collected_at TIMESTAMPTZ NOT NULL,
    reported_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    evidence_s3_key TEXT,
    report_status VARCHAR(20) NOT NULL DEFAULT 'ACTIVE'
        CHECK (report_status IN ('ACTIVE', 'SUPERSEDED', 'VOIDED')),
    supersedes_collection_id BIGINT REFERENCES job_collections(id),
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,

    FOREIGN KEY (job_id, contractor_id)
        REFERENCES jobs (id, contractor_id),
    FOREIGN KEY (reported_by_account_id, contractor_id)
        REFERENCES contractor_accounts (id, contractor_id),
    CHECK (
        supersedes_collection_id IS NULL
        OR supersedes_collection_id <> id
    )
);

CREATE INDEX idx_job_collections_job_date
    ON job_collections (job_id, collected_at);

CREATE INDEX idx_job_collections_contractor_reported
    ON job_collections (contractor_id, reported_at);

CREATE INDEX idx_job_collections_active
    ON job_collections (job_id)
    WHERE report_status = 'ACTIVE';

-- Homeowner responses are additional evidence, not a billing prerequisite.
-- Multiple submissions are retained to preserve late responses and history.
CREATE TABLE homeowner_payment_verifications (
    id BIGSERIAL PRIMARY KEY,
    job_id BIGINT NOT NULL,
    contractor_id BIGINT NOT NULL,
    reported_invoice_total NUMERIC(12, 2)
        CHECK (reported_invoice_total IS NULL OR reported_invoice_total >= 0),
    reported_paid_to_date NUMERIC(12, 2)
        CHECK (reported_paid_to_date IS NULL OR reported_paid_to_date >= 0),
    reported_remaining_balance NUMERIC(12, 2)
        CHECK (
            reported_remaining_balance IS NULL
            OR reported_remaining_balance >= 0
        ),
    work_matches_invoice BOOLEAN,
    comments TEXT,
    discrepancy_detected BOOLEAN NOT NULL DEFAULT FALSE,
    discrepancy_summary TEXT,
    notification_event_key VARCHAR UNIQUE
        REFERENCES outbox_events(event_key),
    submitted_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,

    FOREIGN KEY (job_id, contractor_id)
        REFERENCES jobs (id, contractor_id)
);

CREATE INDEX idx_homeowner_verifications_job
    ON homeowner_payment_verifications (job_id, submitted_at DESC);

CREATE INDEX idx_homeowner_verifications_discrepancies
    ON homeowner_payment_verifications (submitted_at)
    WHERE discrepancy_detected = TRUE;

-- One fee-accrual record per contractor collection report.
-- The application calculates the remaining per-job cap transactionally.
-- Homeowner response status is deliberately not a prerequisite here.
CREATE TABLE job_fee_accruals (
    id BIGSERIAL PRIMARY KEY,
    job_id BIGINT NOT NULL,
    contractor_id BIGINT NOT NULL,
    collection_id BIGINT NOT NULL UNIQUE REFERENCES job_collections(id),
    collection_amount NUMERIC(12, 2) NOT NULL
        CHECK (collection_amount > 0),
    eligible_collection_amount NUMERIC(12, 2) NOT NULL
        CHECK (
            eligible_collection_amount >= 0
            AND eligible_collection_amount <= collection_amount
        ),
    fee_rate NUMERIC(5, 4) NOT NULL DEFAULT 0.1000
        CHECK (fee_rate > 0 AND fee_rate <= 1),
    fee_cap_snapshot NUMERIC(12, 2) NOT NULL DEFAULT 5000.00
        CHECK (fee_cap_snapshot > 0),
    cumulative_fee_before NUMERIC(12, 2) NOT NULL DEFAULT 0
        CHECK (cumulative_fee_before >= 0),
    cumulative_fee_after NUMERIC(12, 2) NOT NULL
        CHECK (cumulative_fee_after >= 0),
    fee_amount NUMERIC(12, 2) NOT NULL CHECK (fee_amount >= 0),
    accrual_status VARCHAR(20) NOT NULL DEFAULT 'ACCRUED'
        CHECK (accrual_status IN ('ACCRUED', 'INVOICED', 'ADJUSTED')),
    verification_status_at_accrual VARCHAR(20) NOT NULL DEFAULT 'NOT_RESPONDED'
        CHECK (
            verification_status_at_accrual IN (
                'NOT_RESPONDED', 'MATCHED', 'DISCREPANCY'
            )
        ),
    accrued_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,

    FOREIGN KEY (job_id, contractor_id)
        REFERENCES jobs (id, contractor_id),
    CHECK (cumulative_fee_after <= fee_cap_snapshot),
    CHECK (cumulative_fee_after >= cumulative_fee_before),
    CHECK (fee_amount <= fee_cap_snapshot)
);

CREATE INDEX idx_job_fee_accruals_unbilled
    ON job_fee_accruals (contractor_id, accrued_at)
    WHERE accrual_status = 'ACCRUED';

CREATE INDEX idx_job_fee_accruals_job
    ON job_fee_accruals (job_id, accrued_at);

-- A submitted dispute does not itself freeze fees.
-- The application applies a freeze only after an approved decision.
CREATE TABLE job_payment_disputes (
    id BIGSERIAL PRIMARY KEY,
    job_id BIGINT NOT NULL,
    contractor_id BIGINT NOT NULL,
    submitted_by_account_id BIGINT NOT NULL,
    reason TEXT NOT NULL CHECK (LENGTH(BTRIM(reason)) > 0),
    status VARCHAR(20) NOT NULL DEFAULT 'SUBMITTED'
        CHECK (
            status IN (
                'SUBMITTED', 'UNDER_REVIEW', 'APPROVED',
                'REJECTED', 'RESOLVED'
            )
        ),
    decision_notes TEXT,
    reviewed_by VARCHAR(255),
    reviewed_at TIMESTAMPTZ,
    fee_freeze_effective_at TIMESTAMPTZ,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,

    FOREIGN KEY (job_id, contractor_id)
        REFERENCES jobs (id, contractor_id),
    FOREIGN KEY (submitted_by_account_id, contractor_id)
        REFERENCES contractor_accounts (id, contractor_id)
);

CREATE INDEX idx_job_payment_disputes_job
    ON job_payment_disputes (job_id, created_at DESC);

CREATE INDEX idx_job_payment_disputes_active
    ON job_payment_disputes (contractor_id, created_at)
    WHERE status IN ('SUBMITTED', 'UNDER_REVIEW', 'APPROVED');

-- Evidence is private object storage; store S3 object keys, not public URLs.
CREATE TABLE job_payment_dispute_evidence (
    id BIGSERIAL PRIMARY KEY,
    dispute_id BIGINT NOT NULL REFERENCES job_payment_disputes(id),
    s3_key TEXT NOT NULL,
    original_filename VARCHAR(255),
    content_type VARCHAR(100),
    uploaded_by_account_id BIGINT NOT NULL
        REFERENCES contractor_accounts(id),
    uploaded_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_job_dispute_evidence_dispute
    ON job_payment_dispute_evidence (dispute_id);

-- Append-only application audit trail for financial actions and reviews.
CREATE TABLE job_financial_audit_events (
    id BIGSERIAL PRIMARY KEY,
    job_id BIGINT NOT NULL REFERENCES jobs(id),
    contractor_id BIGINT NOT NULL REFERENCES contractors(id),
    actor_type VARCHAR(20) NOT NULL
        CHECK (actor_type IN ('CONTRACTOR', 'HOMEOWNER', 'EMG', 'SYSTEM')),
    actor_account_id BIGINT REFERENCES contractor_accounts(id),
    event_type VARCHAR(60) NOT NULL,
    related_record_type VARCHAR(60),
    related_record_id BIGINT,
    details JSONB NOT NULL DEFAULT '{}'::JSONB,
    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_job_financial_audit_job
    ON job_financial_audit_events (job_id, created_at DESC);

COMMIT;
