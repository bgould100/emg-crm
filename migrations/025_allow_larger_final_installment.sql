-- Allow the final installment to exceed the regular installment amount.
-- This is necessary when cent rounding leaves a small residual balance.

BEGIN;

ALTER TABLE job_payment_agreements
    DROP CONSTRAINT job_payment_agreements_payment_terms_check;

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
            AND schedule_type IS NOT NULL
            AND schedule_type IN ('WEEKLY', 'BIWEEKLY', 'MONTHLY', 'CUSTOM')
            AND down_payment_amount IS NOT NULL
            AND down_payment_amount >= 0
            AND down_payment_amount < invoice_total
            AND down_payment_due_date IS NOT NULL
            AND installment_amount IS NOT NULL
            AND installment_amount > 0
            AND installment_count IS NOT NULL
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
                OR final_installment_amount > 0
            )
        )
    );

COMMIT;