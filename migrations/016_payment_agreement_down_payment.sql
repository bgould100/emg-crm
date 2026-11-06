-- Add down-payment and installment-count terms to payment agreements.
-- Preserve existing agreement records and replace the schedule constraint.

BEGIN;

ALTER TABLE job_payment_agreements
    ADD COLUMN down_payment_amount NUMERIC(12,2),
    ADD COLUMN down_payment_due_date DATE,
    ADD COLUMN installment_count INTEGER;

ALTER TABLE job_payment_agreements
    DROP CONSTRAINT job_payment_agreements_check1;

ALTER TABLE job_payment_agreements
    ADD CONSTRAINT job_payment_agreements_payment_terms_check
    CHECK (
        (
            arrangement_type = 'PAID_IN_FULL'
            AND schedule_type IS NULL
            AND installment_amount IS NULL
            AND final_installment_amount IS NULL
            AND plan_start_date IS NULL
            AND final_due_date IS NULL
            AND custom_schedule IS NULL
            AND down_payment_amount IS NULL
            AND down_payment_due_date IS NULL
            AND installment_count IS NULL
        )
        OR
        (
            arrangement_type = 'PAYMENT_PLAN'
            AND schedule_type IN ('WEEKLY', 'BIWEEKLY', 'MONTHLY', 'CUSTOM')
            AND down_payment_amount IS NOT NULL
            AND down_payment_amount >= 0
            AND down_payment_amount < invoice_total
            AND down_payment_due_date IS NOT NULL
            AND installment_amount > 0
            AND installment_count > 0
            AND plan_start_date IS NOT NULL
            AND down_payment_due_date <= plan_start_date
            AND final_due_date IS NOT NULL
            AND final_due_date >= plan_start_date
            AND (
                (schedule_type = 'CUSTOM' AND custom_schedule IS NOT NULL)
                OR
                (schedule_type <> 'CUSTOM' AND custom_schedule IS NULL)
            )
            AND (
                final_installment_amount IS NULL
                OR (
                    final_installment_amount > 0
                    AND final_installment_amount <= installment_amount
                )
            )
        )
    );

ALTER TABLE job_payment_agreements
    ADD CONSTRAINT job_payment_agreements_down_payment_check
    CHECK (
        down_payment_amount IS NULL
        OR down_payment_amount <= invoice_total
    );

ALTER TABLE job_payment_agreements
    ADD CONSTRAINT job_payment_agreements_installment_count_check
    CHECK (
        installment_count IS NULL
        OR installment_count > 0
    );

COMMIT;