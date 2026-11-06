BEGIN;

-- =========================================================
-- 1. Administrative agreement revision support
-- =========================================================

-- An agreement can now be created by either a contractor
-- account or an administrator, but never both.
ALTER TABLE job_payment_agreements
    ALTER COLUMN created_by_account_id DROP NOT NULL;

ALTER TABLE job_payment_agreements
    ADD COLUMN created_by_admin_user_id BIGINT
        REFERENCES admin_users(id),
    ADD COLUMN admin_revision_reason TEXT,
    ADD COLUMN admin_revised_at TIMESTAMPTZ;

ALTER TABLE job_payment_agreements
    DROP CONSTRAINT job_payment_agreements_status_check;

ALTER TABLE job_payment_agreements
    ADD CONSTRAINT job_payment_agreements_status_check
    CHECK (
        status IN (
            'PENDING_ACCEPTANCE',
            'ACCEPTED',
            'SUPERSEDED',
            'ADMIN_REVISED'
        )
    );

-- Preserve original contractor acceptance evidence when an
-- accepted agreement is later superseded.
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
            status IN ('PENDING_ACCEPTANCE', 'ADMIN_REVISED')
            AND accepted_at IS NULL
            AND accepted_by_account_id IS NULL
        )
    );

ALTER TABLE job_payment_agreements
    ADD CONSTRAINT job_payment_agreements_creator_check
    CHECK (
        (
            created_by_account_id IS NOT NULL
            AND created_by_admin_user_id IS NULL
        )
        OR
        (
            created_by_account_id IS NULL
            AND created_by_admin_user_id IS NOT NULL
        )
    );

ALTER TABLE job_payment_agreements
    ADD CONSTRAINT job_payment_agreements_admin_revision_check
    CHECK (
        (
            created_by_admin_user_id IS NULL
            AND admin_revision_reason IS NULL
            AND admin_revised_at IS NULL
        )
        OR
        (
            created_by_admin_user_id IS NOT NULL
            AND status IN ('ADMIN_REVISED', 'SUPERSEDED')
            AND admin_revision_reason IS NOT NULL
            AND length(trim(admin_revision_reason)) > 0
            AND admin_revised_at IS NOT NULL
            AND accepted_at IS NULL
            AND accepted_by_account_id IS NULL
        )
    );

-- Supports composite foreign keys that enforce job consistency.
ALTER TABLE job_payment_agreements
    ADD CONSTRAINT job_payment_agreements_id_job_id_key
    UNIQUE (id, job_id);

-- An accepted contractor agreement or an administratively
-- revised agreement is the one operative agreement for a job.
DROP INDEX IF EXISTS idx_job_payment_agreements_one_accepted_per_job;

CREATE UNIQUE INDEX idx_job_payment_agreements_one_operative_per_job
    ON job_payment_agreements (job_id)
    WHERE status IN ('ACCEPTED', 'ADMIN_REVISED');


-- =========================================================
-- 2. Homeowner review invitations
-- =========================================================

CREATE TABLE homeowner_review_invitations (
    id BIGSERIAL PRIMARY KEY,

    job_id BIGINT NOT NULL UNIQUE
        REFERENCES jobs(id),

    -- Snapshot of the email address at invitation creation.
    customer_email VARCHAR(320),

    delivery_status VARCHAR(30) NOT NULL DEFAULT 'PENDING'
        CHECK (
            delivery_status IN (
                'PENDING',
                'SENT',
                'FAILED',
                'SKIPPED_NO_EMAIL'
            )
        ),

    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    expires_at TIMESTAMPTZ NOT NULL,

    -- SENT means the email provider accepted the message.
    -- It does not claim the message was delivered or opened.
    email_sent_at TIMESTAMPTZ,
    provider_message_id TEXT,

    submitted_at TIMESTAMPTZ,

    attempts INTEGER NOT NULL DEFAULT 0
        CHECK (attempts >= 0),

    last_error TEXT,

    CONSTRAINT homeowner_review_invitation_expiration_check
        CHECK (expires_at > created_at),

    CONSTRAINT homeowner_review_invitation_no_email_check
        CHECK (
            delivery_status <> 'SKIPPED_NO_EMAIL'
            OR customer_email IS NULL
        ),

    CONSTRAINT homeowner_review_invitation_sent_check
        CHECK (
            delivery_status <> 'SENT'
            OR email_sent_at IS NOT NULL
        ),

    CONSTRAINT homeowner_review_invitations_id_job_id_key
        UNIQUE (id, job_id)
);

CREATE INDEX idx_homeowner_review_invitations_pending
    ON homeowner_review_invitations (created_at)
    WHERE delivery_status IN ('PENDING', 'FAILED');


-- =========================================================
-- 3. Homeowner reviews
-- =========================================================

CREATE TABLE homeowner_reviews (
    id BIGSERIAL PRIMARY KEY,

    invitation_id BIGINT NOT NULL UNIQUE,
    job_id BIGINT NOT NULL UNIQUE,

    rating SMALLINT NOT NULL
        CHECK (rating BETWEEN 1 AND 5),

    written_feedback TEXT,

    -- Zero is the storage default, not evidence that the
    -- homeowner reported a zero-dollar job.
    total_cost NUMERIC(12,2) NOT NULL DEFAULT 0
        CHECK (total_cost >= 0),

    total_cost_submitted BOOLEAN NOT NULL DEFAULT FALSE,

    payment_plan_agreed BOOLEAN NOT NULL,

    cost_comments TEXT,

    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT homeowner_reviews_cost_submission_check
        CHECK (
            total_cost_submitted
            OR total_cost = 0
        ),

    CONSTRAINT homeowner_reviews_invitation_job_fkey
        FOREIGN KEY (invitation_id, job_id)
        REFERENCES homeowner_review_invitations (id, job_id),

    CONSTRAINT homeowner_reviews_id_job_id_key
        UNIQUE (id, job_id)
);


-- =========================================================
-- 4. Homeowner review discrepancies
-- =========================================================

CREATE TABLE homeowner_review_discrepancies (
    id BIGSERIAL PRIMARY KEY,

    review_id BIGINT NOT NULL,
    job_id BIGINT NOT NULL,
    agreement_id BIGINT NOT NULL,

    discrepancy_type VARCHAR(40) NOT NULL
        CHECK (
            discrepancy_type IN (
                'TOTAL_COST',
                'PAYMENT_PLAN'
            )
        ),

    status VARCHAR(30) NOT NULL DEFAULT 'OPEN'
        CHECK (
            status IN (
                'OPEN',
                'UNDER_REVIEW',
                'RESOLVED',
                'DISMISSED'
            )
        ),

    -- Snapshot of the figures and conditions that caused
    -- the discrepancy to be created.
    details JSONB NOT NULL DEFAULT '{}'::jsonb,

    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,

    resolved_at TIMESTAMPTZ,
    resolved_by_admin_user_id BIGINT REFERENCES admin_users(id),
    resolution_notes TEXT,

    CONSTRAINT homeowner_review_discrepancy_review_job_fkey
        FOREIGN KEY (review_id, job_id)
        REFERENCES homeowner_reviews (id, job_id),

    CONSTRAINT homeowner_review_discrepancy_agreement_job_fkey
        FOREIGN KEY (agreement_id, job_id)
        REFERENCES job_payment_agreements (id, job_id),

    CONSTRAINT homeowner_review_discrepancy_resolution_check
        CHECK (
            (
                status IN ('OPEN', 'UNDER_REVIEW')
                AND resolved_at IS NULL
                AND resolved_by_admin_user_id IS NULL
                AND resolution_notes IS NULL
            )
            OR
            (
                status IN ('RESOLVED', 'DISMISSED')
                AND resolved_at IS NOT NULL
                AND resolved_by_admin_user_id IS NOT NULL
                AND resolution_notes IS NOT NULL
                AND length(trim(resolution_notes)) > 0
            )
        ),

    UNIQUE (review_id, discrepancy_type)
);

CREATE INDEX idx_homeowner_review_discrepancies_admin
    ON homeowner_review_discrepancies (status, created_at DESC);

CREATE INDEX idx_homeowner_review_discrepancies_job
    ON homeowner_review_discrepancies (job_id);


COMMIT;
