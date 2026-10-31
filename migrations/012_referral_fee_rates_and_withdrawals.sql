-- Establish contractor defaults, opportunity overrides, and accepted-job fee snapshots.
BEGIN;

ALTER TABLE contractors
    ADD COLUMN IF NOT EXISTS default_referral_fee_rate NUMERIC(5,4)
    NOT NULL DEFAULT 0.1000;

ALTER TABLE opportunities
    ADD COLUMN IF NOT EXISTS referral_fee_rate_override NUMERIC(5,4);

-- Existing jobs retain the current 10% agreement regardless of future contractor defaults.
ALTER TABLE jobs
    ADD COLUMN IF NOT EXISTS referral_fee_rate NUMERIC(5,4)
    NOT NULL DEFAULT 0.1000;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'contractors_default_referral_fee_rate_check'
    ) THEN
        ALTER TABLE contractors
            ADD CONSTRAINT contractors_default_referral_fee_rate_check
            CHECK (default_referral_fee_rate IN (0.0000, 0.0500, 0.1000));
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'opportunities_referral_fee_rate_override_check'
    ) THEN
        ALTER TABLE opportunities
            ADD CONSTRAINT opportunities_referral_fee_rate_override_check
            CHECK (
                referral_fee_rate_override IS NULL
                OR referral_fee_rate_override IN (0.0000, 0.0500, 0.1000)
            );
    END IF;

    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'jobs_referral_fee_rate_check'
    ) THEN
        ALTER TABLE jobs
            ADD CONSTRAINT jobs_referral_fee_rate_check
            CHECK (referral_fee_rate IN (0.0000, 0.0500, 0.1000));
    END IF;
END
$$;

-- Identify the administrator responsible for financial changes.
ALTER TABLE job_financial_audit_events
    ADD COLUMN IF NOT EXISTS admin_user_id BIGINT
    REFERENCES admin_users(id);

ALTER TABLE job_financial_audit_events
    DROP CONSTRAINT IF EXISTS job_financial_audit_events_actor_type_check;

ALTER TABLE job_financial_audit_events
    ADD CONSTRAINT job_financial_audit_events_actor_type_check
    CHECK (actor_type IN ('CONTRACTOR', 'HOMEOWNER', 'EMG', 'SYSTEM', 'ADMIN'));

-- Permit a new historical opportunity after a prior invitation is withdrawn
-- or an accepted bid slot is explicitly released.
ALTER TABLE opportunities
    DROP CONSTRAINT IF EXISTS opportunities_lead_id_contractor_id_key;

CREATE UNIQUE INDEX IF NOT EXISTS opportunities_one_active_contractor_per_lead
    ON opportunities (lead_id, contractor_id)
    WHERE status IN ('PENDING', 'ACCEPTED')
      AND slot_released = FALSE;

COMMIT;
