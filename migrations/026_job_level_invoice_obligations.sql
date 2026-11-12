BEGIN;

-- ============================================================
-- 1. COMPOSITE KEYS FOR CONTRACTOR-SAFE RELATIONSHIPS
-- ============================================================

ALTER TABLE contractor_invoices
    ADD CONSTRAINT uq_contractor_invoices_id_contractor
    UNIQUE (id, contractor_id);

ALTER TABLE jobs
    ADD CONSTRAINT uq_jobs_id_contractor
    UNIQUE (id, contractor_id);

ALTER TABLE job_fee_accruals
    ADD CONSTRAINT uq_job_fee_accruals_id_job_contractor
    UNIQUE (id, job_id, contractor_id);

COMMENT ON COLUMN contractor_invoices.total_amount IS
    'Gross amount originally issued on the invoice. Subsequent adjustments and credits are recorded separately.';

-- ============================================================
-- PROTECT THE ORIGINAL INVOICE TOTAL AFTER ISSUANCE
-- ============================================================

CREATE FUNCTION emg_guard_contractor_invoice_total()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    IF NEW.total_amount IS DISTINCT FROM OLD.total_amount
       AND (
           OLD.status <> 'DRAFT'
           OR NEW.status <> 'DRAFT'
       ) THEN
        RAISE EXCEPTION
            'The original invoice total cannot be changed after issuance';
    END IF;

    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_contractor_invoice_total_immutable
BEFORE UPDATE OF total_amount
ON contractor_invoices
FOR EACH ROW
EXECUTE FUNCTION emg_guard_contractor_invoice_total();

-- ============================================================
-- 2. CONSOLIDATED JOB-LEVEL INVOICE OBLIGATIONS
-- ============================================================

CREATE TABLE contractor_invoice_obligations (
    id BIGSERIAL PRIMARY KEY,
    invoice_id BIGINT NOT NULL,
    job_id BIGINT NOT NULL,
    contractor_id BIGINT NOT NULL,

    original_amount NUMERIC(12,2) NOT NULL DEFAULT 0
        CHECK (original_amount >= 0),

    current_amount NUMERIC(12,2) NOT NULL DEFAULT 0
        CHECK (current_amount >= 0),

    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,

    UNIQUE (invoice_id, job_id),
    UNIQUE (id, job_id, contractor_id),

    FOREIGN KEY (invoice_id, contractor_id)
        REFERENCES contractor_invoices (id, contractor_id),

    FOREIGN KEY (job_id, contractor_id)
        REFERENCES jobs (id, contractor_id)
);

CREATE INDEX idx_invoice_obligations_contractor
    ON contractor_invoice_obligations (contractor_id, invoice_id);

CREATE INDEX idx_invoice_obligations_job
    ON contractor_invoice_obligations (job_id);

COMMENT ON TABLE contractor_invoice_obligations IS
    'Consolidated job-level obligations on contractor invoices. Original amounts are preserved; subsequent changes are recorded as adjustments and credit applications.';


-- ============================================================
-- 3. LINK ORIGINAL FEE ACCRUALS TO INVOICE OBLIGATIONS
-- ============================================================

CREATE TABLE contractor_invoice_obligation_accruals (
    id BIGSERIAL PRIMARY KEY,
    obligation_id BIGINT NOT NULL,
    fee_accrual_id BIGINT NOT NULL,
    job_id BIGINT NOT NULL,
    contractor_id BIGINT NOT NULL,

    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,

    UNIQUE (fee_accrual_id),

    FOREIGN KEY (obligation_id, job_id, contractor_id)
        REFERENCES contractor_invoice_obligations
            (id, job_id, contractor_id),

    FOREIGN KEY (fee_accrual_id, job_id, contractor_id)
        REFERENCES job_fee_accruals
            (id, job_id, contractor_id)
);

CREATE INDEX idx_obligation_accruals_obligation
    ON contractor_invoice_obligation_accruals (obligation_id);

COMMENT ON TABLE contractor_invoice_obligation_accruals IS
    'Maps original fee accruals to consolidated invoice obligations. Each accrual can be assigned to only one obligation.';


-- ============================================================
-- 4. PROTECT OBLIGATION AMOUNTS
--
-- Amounts are derived from:
--   original_amount = sum of linked original fee accruals
--   current_amount  = original_amount + sum of adjustments
--
-- Ordinary updates cannot independently change these amounts.
-- ============================================================

CREATE FUNCTION emg_guard_invoice_obligation_update()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
DECLARE
    v_invoice_status VARCHAR(20);
    v_original_amount NUMERIC(12,2);
    v_adjustment_total NUMERIC(12,2);
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION
            'Invoice obligations cannot be deleted';
    END IF;

    -- New obligations must start with zero balances and belong
    -- to draft invoices. Locking the invoice also serializes
    -- obligation creation against invoice issuance.
    IF TG_OP = 'INSERT' THEN

        IF NEW.original_amount <> 0
           OR NEW.current_amount <> 0 THEN
            RAISE EXCEPTION
                'New invoice obligations must start with zero balances';
        END IF;

        SELECT status
          INTO v_invoice_status
          FROM contractor_invoices
         WHERE id = NEW.invoice_id
           AND contractor_id = NEW.contractor_id
         FOR UPDATE;

        IF NOT FOUND THEN
            RAISE EXCEPTION
                'Invoice % was not found for contractor %',
                NEW.invoice_id,
                NEW.contractor_id;
        END IF;

        IF v_invoice_status <> 'DRAFT' THEN
            RAISE EXCEPTION
                'New obligations can only be created for draft invoices';
        END IF;

        RETURN NEW;
    END IF;

    -- Identity fields cannot be changed after creation.
    IF NEW.id IS DISTINCT FROM OLD.id
       OR NEW.invoice_id IS DISTINCT FROM OLD.invoice_id
       OR NEW.job_id IS DISTINCT FROM OLD.job_id
       OR NEW.contractor_id IS DISTINCT FROM OLD.contractor_id
       OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
        RAISE EXCEPTION
            'Invoice obligation identity fields cannot be changed';
    END IF;

    -- Derive the original amount from the immutable accrual links.
    SELECT COALESCE(SUM(a.fee_amount), 0)
      INTO v_original_amount
      FROM contractor_invoice_obligation_accruals l
      JOIN job_fee_accruals a
        ON a.id = l.fee_accrual_id
       AND a.job_id = l.job_id
       AND a.contractor_id = l.contractor_id
     WHERE l.obligation_id = NEW.id;

    -- Derive all subsequent changes from the adjustment ledger.
    SELECT COALESCE(SUM(adjustment_amount), 0)
      INTO v_adjustment_total
      FROM contractor_invoice_adjustments
     WHERE obligation_id = NEW.id;

    IF NEW.original_amount IS DISTINCT FROM v_original_amount THEN
        RAISE EXCEPTION
            'Original obligation amount (%) must equal its accrual ledger total (%)',
            NEW.original_amount,
            v_original_amount;
    END IF;

    IF NEW.current_amount IS DISTINCT FROM
       (v_original_amount + v_adjustment_total) THEN
        RAISE EXCEPTION
            'Current obligation amount (%) must equal its ledger-derived balance (%)',
            NEW.current_amount,
            (v_original_amount + v_adjustment_total);
    END IF;

    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_guard_invoice_obligation_update
BEFORE INSERT OR UPDATE OR DELETE
ON contractor_invoice_obligations
FOR EACH ROW
EXECUTE FUNCTION emg_guard_invoice_obligation_update();


-- ============================================================
-- 5. VALIDATE ACCRUAL ASSIGNMENTS BEFORE INSERTION
--
-- Lock order:
--   1. Invoice
--   2. Fee accrual
--   3. Obligation update
--
-- This prevents accrual assignment from racing invoice issuance.
-- ============================================================

CREATE FUNCTION emg_validate_invoice_obligation_accrual()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
DECLARE
    v_invoice_status VARCHAR(20);
    v_accrual_status TEXT;
BEGIN
    -- Lock the invoice first.
    SELECT i.status
      INTO v_invoice_status
      FROM contractor_invoice_obligations o
      JOIN contractor_invoices i
        ON i.id = o.invoice_id
       AND i.contractor_id = o.contractor_id
     WHERE o.id = NEW.obligation_id
       AND o.job_id = NEW.job_id
       AND o.contractor_id = NEW.contractor_id
     FOR UPDATE OF i;

    IF NOT FOUND THEN
        RAISE EXCEPTION
            'Invoice obligation % or its invoice was not found',
            NEW.obligation_id;
    END IF;

    IF v_invoice_status <> 'DRAFT' THEN
        RAISE EXCEPTION
            'Fee accruals can only be assigned to draft invoices';
    END IF;

    -- Lock and validate the fee accrual.
    SELECT accrual_status
      INTO v_accrual_status
      FROM job_fee_accruals
     WHERE id = NEW.fee_accrual_id
       AND job_id = NEW.job_id
       AND contractor_id = NEW.contractor_id
     FOR UPDATE;

    IF NOT FOUND THEN
        RAISE EXCEPTION
            'Fee accrual % was not found for the specified job and contractor',
            NEW.fee_accrual_id;
    END IF;

    IF v_accrual_status <> 'ACCRUED' THEN
        RAISE EXCEPTION
            'Only ACCRUED fee records may be assigned to a new invoice obligation';
    END IF;

    IF EXISTS (
        SELECT 1
          FROM contractor_invoice_items
         WHERE fee_accrual_id = NEW.fee_accrual_id
    ) THEN
        RAISE EXCEPTION
            'Fee accrual % is already assigned to a legacy invoice item',
            NEW.fee_accrual_id;
    END IF;

    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_validate_invoice_obligation_accrual
BEFORE INSERT ON contractor_invoice_obligation_accruals
FOR EACH ROW
EXECUTE FUNCTION emg_validate_invoice_obligation_accrual();

-- =========================================================
-- 5B. PREVENT LEGACY ASSIGNMENT OF OBLIGATION ACCRUALS
--
-- The new obligation assignment guard already prevents
-- accruals assigned to legacy invoice items from entering
-- the new system.
--
-- This trigger provides the reverse protection.
-- Both paths lock the invoice first and the accrual second.
-- =========================================================

CREATE FUNCTION emg_validate_legacy_invoice_item()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
DECLARE
    v_invoice_id BIGINT;
BEGIN
    -- Lock the legacy invoice first.
    SELECT id
      INTO v_invoice_id
      FROM contractor_invoices
     WHERE id = NEW.invoice_id
     FOR UPDATE;

    IF NOT FOUND THEN
        RAISE EXCEPTION
            'Legacy invoice % was not found',
            NEW.invoice_id;
    END IF;

    -- Lock the fee accrual.
    PERFORM 1
      FROM job_fee_accruals
     WHERE id = NEW.fee_accrual_id
     FOR UPDATE;

    IF NOT FOUND THEN
        RAISE EXCEPTION
            'Fee accrual % was not found',
            NEW.fee_accrual_id;
    END IF;

    -- Reject accruals already assigned to the new system.
    IF EXISTS (
        SELECT 1
          FROM contractor_invoice_obligation_accruals
         WHERE fee_accrual_id = NEW.fee_accrual_id
    ) THEN
        RAISE EXCEPTION
            'Fee accrual % is already assigned to a new invoice obligation',
            NEW.fee_accrual_id;
    END IF;

    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_validate_legacy_invoice_item
BEFORE INSERT OR UPDATE OF invoice_id, fee_accrual_id
ON contractor_invoice_items
FOR EACH ROW
EXECUTE FUNCTION emg_validate_legacy_invoice_item();


-- ============================================================
-- 6. APPLY ACCRUAL ASSIGNMENTS
--
-- The link record now exists, so the obligation guard can
-- validate both amounts against the actual accrual ledger.
-- ============================================================

CREATE FUNCTION emg_apply_invoice_obligation_accrual()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
DECLARE
    v_fee_amount NUMERIC(12,2);
BEGIN
    SELECT fee_amount
      INTO v_fee_amount
      FROM job_fee_accruals
     WHERE id = NEW.fee_accrual_id
       AND job_id = NEW.job_id
       AND contractor_id = NEW.contractor_id;

    IF NOT FOUND THEN
        RAISE EXCEPTION
            'Fee accrual % was not found',
            NEW.fee_accrual_id;
    END IF;

    UPDATE contractor_invoice_obligations
       SET original_amount = original_amount + v_fee_amount,
           current_amount = current_amount + v_fee_amount,
           updated_at = clock_timestamp()
     WHERE id = NEW.obligation_id
       AND job_id = NEW.job_id
       AND contractor_id = NEW.contractor_id;

    IF NOT FOUND THEN
        RAISE EXCEPTION
            'Invoice obligation % was not found',
            NEW.obligation_id;
    END IF;

    UPDATE job_fee_accruals
       SET accrual_status = 'INVOICED'
     WHERE id = NEW.fee_accrual_id
       AND job_id = NEW.job_id
       AND contractor_id = NEW.contractor_id;

    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_apply_invoice_obligation_accrual
AFTER INSERT ON contractor_invoice_obligation_accruals
FOR EACH ROW
EXECUTE FUNCTION emg_apply_invoice_obligation_accrual();


-- Accrual assignments are permanent.
-- Corrections must use financial adjustment records.
CREATE FUNCTION emg_reject_obligation_accrual_changes()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION
        'Invoice obligation accrual assignments cannot be updated or deleted';
END;
$$;

CREATE TRIGGER trg_immutable_obligation_accruals
BEFORE UPDATE OR DELETE
ON contractor_invoice_obligation_accruals
FOR EACH ROW
EXECUTE FUNCTION emg_reject_obligation_accrual_changes();


-- ============================================================
-- 7. ADMINISTRATIVE ADJUSTMENTS
-- ============================================================

CREATE TABLE contractor_invoice_adjustments (
    id BIGSERIAL PRIMARY KEY,
    obligation_id BIGINT NOT NULL,
    job_id BIGINT NOT NULL,
    contractor_id BIGINT NOT NULL,

    previous_amount NUMERIC(12,2) NOT NULL
        CHECK (previous_amount >= 0),

    new_amount NUMERIC(12,2) NOT NULL
        CHECK (new_amount >= 0),

    adjustment_amount NUMERIC(12,2) NOT NULL,

    reason TEXT NOT NULL
        CHECK (length(btrim(reason)) > 0),

    admin_user_id BIGINT NOT NULL
        REFERENCES admin_users(id),

    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CHECK (adjustment_amount = new_amount - previous_amount),
    CHECK (adjustment_amount <> 0),

    FOREIGN KEY (obligation_id, job_id, contractor_id)
        REFERENCES contractor_invoice_obligations
            (id, job_id, contractor_id)
);

CREATE INDEX idx_invoice_adjustments_obligation
    ON contractor_invoice_adjustments (obligation_id, created_at DESC);

CREATE INDEX idx_invoice_adjustments_contractor
    ON contractor_invoice_adjustments (contractor_id, created_at DESC);


-- ============================================================
-- 8. VALIDATE ADJUSTMENTS BEFORE INSERTION
--
-- Adjustments are allowed only on issued invoices.
-- The invoice and obligation are locked before the ledger
-- record is inserted, preventing concurrent balance changes.
-- ============================================================

CREATE FUNCTION emg_validate_invoice_adjustment()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
DECLARE
    v_invoice_status VARCHAR(20);
    v_current_amount NUMERIC(12,2);
BEGIN
    -- Lock the invoice first.
    SELECT i.status
      INTO v_invoice_status
      FROM contractor_invoice_obligations o
      JOIN contractor_invoices i
        ON i.id = o.invoice_id
       AND i.contractor_id = o.contractor_id
     WHERE o.id = NEW.obligation_id
       AND o.job_id = NEW.job_id
       AND o.contractor_id = NEW.contractor_id
     FOR UPDATE OF i;

    IF NOT FOUND THEN
        RAISE EXCEPTION
            'Invoice obligation % or its invoice was not found',
            NEW.obligation_id;
    END IF;

    IF v_invoice_status <> 'SENT' THEN
        RAISE EXCEPTION
            'Adjustments are only permitted on unpaid, issued invoices; current invoice status is %',
            v_invoice_status;
    END IF;

    -- Lock the obligation and verify its current balance.
    SELECT current_amount
      INTO v_current_amount
      FROM contractor_invoice_obligations
     WHERE id = NEW.obligation_id
       AND job_id = NEW.job_id
       AND contractor_id = NEW.contractor_id
     FOR UPDATE;

    IF NOT FOUND THEN
        RAISE EXCEPTION
            'Invoice obligation % was not found',
            NEW.obligation_id;
    END IF;

    IF NEW.previous_amount <> v_current_amount THEN
        RAISE EXCEPTION
            'Adjustment previous amount (%) does not match current obligation amount (%)',
            NEW.previous_amount,
            v_current_amount;
    END IF;

    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_validate_invoice_adjustment
BEFORE INSERT ON contractor_invoice_adjustments
FOR EACH ROW
EXECUTE FUNCTION emg_validate_invoice_adjustment();


-- ============================================================
-- 9. APPLY ADJUSTMENTS AFTER LEDGER INSERTION
--
-- The adjustment record now exists, so the obligation guard
-- can verify the new balance against the complete ledger.
-- ============================================================

CREATE FUNCTION emg_apply_invoice_adjustment()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    UPDATE contractor_invoice_obligations
       SET current_amount = NEW.new_amount,
           updated_at = clock_timestamp()
     WHERE id = NEW.obligation_id
       AND job_id = NEW.job_id
       AND contractor_id = NEW.contractor_id;

    IF NOT FOUND THEN
        RAISE EXCEPTION
            'Invoice obligation % was not found',
            NEW.obligation_id;
    END IF;

    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_apply_invoice_adjustment
AFTER INSERT ON contractor_invoice_adjustments
FOR EACH ROW
EXECUTE FUNCTION emg_apply_invoice_adjustment();


-- Adjustment history is append-only.
CREATE FUNCTION emg_reject_invoice_adjustment_changes()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION
        'Invoice adjustment records are immutable; create a new adjustment instead';
END;
$$;

CREATE TRIGGER trg_immutable_invoice_adjustments
BEFORE UPDATE OR DELETE ON contractor_invoice_adjustments
FOR EACH ROW
EXECUTE FUNCTION emg_reject_invoice_adjustment_changes();

-- ============================================================
-- 10. PROTECT ORIGINAL FEE ACCRUAL SNAPSHOTS
-- ============================================================

CREATE FUNCTION emg_guard_job_fee_accrual_update()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    IF NEW.id IS DISTINCT FROM OLD.id
       OR NEW.job_id IS DISTINCT FROM OLD.job_id
       OR NEW.contractor_id IS DISTINCT FROM OLD.contractor_id
       OR NEW.collection_id IS DISTINCT FROM OLD.collection_id
       OR NEW.collection_amount IS DISTINCT FROM OLD.collection_amount
       OR NEW.eligible_collection_amount IS DISTINCT FROM OLD.eligible_collection_amount
       OR NEW.fee_rate IS DISTINCT FROM OLD.fee_rate
       OR NEW.fee_cap_snapshot IS DISTINCT FROM OLD.fee_cap_snapshot
       OR NEW.cumulative_fee_before IS DISTINCT FROM OLD.cumulative_fee_before
       OR NEW.cumulative_fee_after IS DISTINCT FROM OLD.cumulative_fee_after
       OR NEW.fee_amount IS DISTINCT FROM OLD.fee_amount
       OR NEW.verification_status_at_accrual IS DISTINCT FROM OLD.verification_status_at_accrual
       OR NEW.accrued_at IS DISTINCT FROM OLD.accrued_at THEN
        RAISE EXCEPTION
            'Original fee accrual calculation and identity fields are immutable';
    END IF;

    IF OLD.accrual_status <> NEW.accrual_status
       AND NOT (
           OLD.accrual_status = 'ACCRUED'
           AND NEW.accrual_status = 'INVOICED'
       ) THEN
        RAISE EXCEPTION
            'Invalid fee accrual status transition: % to %',
            OLD.accrual_status,
            NEW.accrual_status;
    END IF;

    IF OLD.accrual_status = 'ACCRUED'
       AND NEW.accrual_status = 'INVOICED'
       AND NOT EXISTS (
           SELECT 1
             FROM contractor_invoice_obligation_accruals
            WHERE fee_accrual_id = NEW.id
       ) THEN
        RAISE EXCEPTION
            'A fee accrual cannot be marked INVOICED without an invoice obligation assignment';
    END IF;

    RETURN NEW;
END;
$$;

CREATE TRIGGER trg_guard_job_fee_accrual_update
BEFORE UPDATE ON job_fee_accruals
FOR EACH ROW
EXECUTE FUNCTION emg_guard_job_fee_accrual_update();

CREATE TRIGGER trg_guard_job_fee_accrual_delete
BEFORE DELETE ON job_fee_accruals
FOR EACH ROW
EXECUTE FUNCTION emg_reject_obligation_accrual_changes();

-- ============================================================
-- 11. RETAIN LEGACY INVOICE ITEMS
-- ============================================================

COMMENT ON TABLE contractor_invoice_items IS
    'Legacy accrual-level invoice items. New invoicing uses contractor_invoice_obligations and contractor_invoice_obligation_accruals. Retained for compatibility and historical preservation.';


COMMIT;