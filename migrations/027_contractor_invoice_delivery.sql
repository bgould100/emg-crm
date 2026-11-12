
BEGIN;

-- ============================================================
-- 027: Contractor invoice delivery tracking
--
-- Delivery state is deliberately separate from invoice state.
-- An invoice remains DRAFT until the email provider confirms
-- acceptance. Delivery failures never create another invoice.
-- ============================================================

CREATE TABLE contractor_invoice_deliveries (
    id BIGSERIAL PRIMARY KEY,

    invoice_id BIGINT NOT NULL UNIQUE
        REFERENCES contractor_invoices(id),

    -- Snapshot of the contractor's profile email at preparation.
    -- NULL is permitted only for blocked deliveries.
    recipient_email VARCHAR(255),

    delivery_status VARCHAR(20) NOT NULL DEFAULT 'PENDING'
        CHECK (
            delivery_status IN (
                'PENDING',
                'IN_PROGRESS',
                'SENT',
                'FAILED',
                'UNKNOWN',
                'PAUSED',
                'BLOCKED'
            )
        ),

    attempt_count INTEGER NOT NULL DEFAULT 0
        CHECK (attempt_count >= 0),

    failure_streak INTEGER NOT NULL DEFAULT 0
        CHECK (failure_streak >= 0),

    next_attempt_at TIMESTAMPTZ,

    last_attempt_at TIMESTAMPTZ,

    claimed_until TIMESTAMPTZ,

    claim_token UUID,

    provider_message_id TEXT,

    last_error TEXT,

    paused_at TIMESTAMPTZ,

    resolved_at TIMESTAMPTZ,

    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,

    updated_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP,

    CONSTRAINT contractor_invoice_deliveries_recipient_check
        CHECK (
            (
                delivery_status = 'BLOCKED'
                AND (
                    recipient_email IS NULL
                    OR BTRIM(recipient_email) = ''
                )
            )
            OR
            (
                delivery_status <> 'BLOCKED'
                AND recipient_email IS NOT NULL
                AND BTRIM(recipient_email) <> ''
            )
        ),

    CONSTRAINT contractor_invoice_deliveries_claim_check
        CHECK (
            (
                delivery_status = 'IN_PROGRESS'
                AND claimed_until IS NOT NULL
                AND claim_token IS NOT NULL
            )
            OR
            (
                delivery_status <> 'IN_PROGRESS'
                AND claimed_until IS NULL
                AND claim_token IS NULL
            )
        ),

    CONSTRAINT contractor_invoice_deliveries_sent_check
        CHECK (
            delivery_status <> 'SENT'
            OR (
                provider_message_id IS NOT NULL
                AND BTRIM(provider_message_id) <> ''
            )
        )
);

-- Workers retrieve only deliveries eligible for processing.
CREATE INDEX idx_contractor_invoice_deliveries_due
    ON contractor_invoice_deliveries (
        next_attempt_at,
        created_at,
        id
    )
    WHERE delivery_status IN ('PENDING', 'FAILED');

-- Supports inspection of claims that may need administrative review.
CREATE INDEX idx_contractor_invoice_deliveries_claimed
    ON contractor_invoice_deliveries (claimed_until)
    WHERE delivery_status = 'IN_PROGRESS';

-- ============================================================
-- Append-only delivery event history
-- ============================================================

CREATE TABLE contractor_invoice_delivery_events (
    id BIGSERIAL PRIMARY KEY,

    delivery_id BIGINT NOT NULL
        REFERENCES contractor_invoice_deliveries(id),

    event_type VARCHAR(30) NOT NULL
        CHECK (
            event_type IN (
                'CREATED',
                'ATTEMPT_STARTED',
                'ATTEMPT_SUCCEEDED',
                'ATTEMPT_FAILED',
                'OUTCOME_UNKNOWN',
                'CLAIM_EXPIRED',
                'PAUSED',
                'RESUMED',
                'BLOCKED',
                'RECIPIENT_UPDATED',
                'ADMIN_RESOLVED',
                'ADMIN_RESEND'
            )
        ),

    attempt_number INTEGER
        CHECK (attempt_number IS NULL OR attempt_number >= 0),

    admin_user_id BIGINT
        REFERENCES admin_users(id),

    details JSONB NOT NULL DEFAULT '{}'::jsonb,

    created_at TIMESTAMPTZ NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX idx_contractor_invoice_delivery_events_delivery
    ON contractor_invoice_delivery_events (
        delivery_id,
        created_at,
        id
    );

-- Prevent alteration or deletion of the delivery audit history.
CREATE OR REPLACE FUNCTION emg_prevent_contractor_invoice_delivery_event_mutation()
RETURNS TRIGGER
LANGUAGE plpgsql
AS $$
BEGIN
    RAISE EXCEPTION
        'Contractor invoice delivery events are append-only';
END;
$$;

CREATE TRIGGER trg_contractor_invoice_delivery_events_append_only
BEFORE UPDATE OR DELETE
ON contractor_invoice_delivery_events
FOR EACH ROW
EXECUTE FUNCTION emg_prevent_contractor_invoice_delivery_event_mutation();

COMMIT;
