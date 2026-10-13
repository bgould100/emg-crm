from decimal import Decimal, ROUND_HALF_UP
from datetime import date
from psycopg.types.json import Jsonb

from app.database.connection import get_connection
CENT = Decimal("0.01")


def _money(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)

class AgreementJobNotFoundError(Exception):
    pass


class InvalidAgreementError(ValueError):
    pass


def _money(value):
    return Decimal(value).quantize(Decimal("0.01"))


def _agreement_dict(row):
    if row is None:
        return None

    result = dict(row)
    for field in ("invoice_total", "installment_amount", "final_installment_amount"):
        if result.get(field) is not None:
            result[field] = str(result[field])

    for field in ("plan_start_date", "final_due_date", "paid_in_full_at", "created_at"):
        if result.get(field) is not None:
            result[field] = result[field].isoformat()

    return result


def _validate_terms(
    arrangement_type,
    invoice_total,
    schedule_type,
    installment_amount,
    final_installment_amount,
    plan_start_date,
    final_due_date,
    custom_schedule,
):
    if not isinstance(invoice_total, Decimal) or not invoice_total.is_finite():
        raise InvalidAgreementError("Invoice total must be a valid amount.")

    if invoice_total <= 0 or invoice_total != _money(invoice_total):
        raise InvalidAgreementError(
            "Invoice total must be positive and have no more than two decimal places."
        )

    if arrangement_type == "PAID_IN_FULL":
        if any(value is not None for value in (
            schedule_type,
            installment_amount,
            final_installment_amount,
            plan_start_date,
            final_due_date,
            custom_schedule,
        )):
            raise InvalidAgreementError(
                "Paid-in-full agreements cannot include installment terms."
            )
        return

    if arrangement_type != "PAYMENT_PLAN":
        raise InvalidAgreementError("Unsupported payment arrangement.")

    if schedule_type not in ("WEEKLY", "BIWEEKLY", "MONTHLY", "CUSTOM"):
        raise InvalidAgreementError("Choose a valid payment schedule.")

    if not isinstance(installment_amount, Decimal) or not installment_amount.is_finite():
        raise InvalidAgreementError("Installment amount must be a valid amount.")

    if installment_amount <= 0 or installment_amount != _money(installment_amount):
        raise InvalidAgreementError(
            "Installment amount must be positive and have no more than two decimal places."
        )

    if installment_amount > invoice_total:
        raise InvalidAgreementError(
            "Installment amount cannot exceed the total invoice amount."
        )

    if final_installment_amount is not None:
        if (
            not isinstance(final_installment_amount, Decimal)
            or not final_installment_amount.is_finite()
            or final_installment_amount <= 0
            or final_installment_amount != _money(final_installment_amount)
        ):
            raise InvalidAgreementError(
                "Final installment must be positive and have no more than two decimal places."
            )
        if final_installment_amount > installment_amount:
            raise InvalidAgreementError(
                "Final installment cannot exceed the regular installment amount."
            )

    if plan_start_date is None or final_due_date is None:
        raise InvalidAgreementError(
            "Payment plans require a start date and final due date."
        )

    if final_due_date < plan_start_date:
        raise InvalidAgreementError(
            "Final due date cannot be before the plan start date."
        )

    if schedule_type == "CUSTOM":
        if not isinstance(custom_schedule, list) or not custom_schedule:
            raise InvalidAgreementError(
                "Custom schedules require at least one schedule entry."
            )

        schedule_total = Decimal("0.00")
        previous_due_date = None
        last_schedule_amount = None

        for item in custom_schedule:
            if (
                not isinstance(item, dict)
                or not isinstance(item.get("due_date"), str)
                or not item.get("amount")
            ):
                raise InvalidAgreementError(
                    "Each custom schedule entry requires due_date and amount."
                )

            try:
                due_date = date.fromisoformat(item["due_date"])
                amount = Decimal(str(item["amount"]))
            except (ValueError, TypeError):
                raise InvalidAgreementError(
                    "Custom schedule dates and amounts must be valid."
                )

            if not amount.is_finite() or amount <= 0 or amount != _money(amount):
                raise InvalidAgreementError(
                    "Custom schedule amounts must be positive with no more than two decimal places."
                )

            if due_date < plan_start_date or due_date > final_due_date:
                raise InvalidAgreementError(
                    "Custom schedule dates must fall within the plan dates."
                )

            if previous_due_date is not None and due_date <= previous_due_date:
                raise InvalidAgreementError(
                    "Custom schedule dates must be strictly increasing with no duplicates."
                )

            schedule_total += amount
            previous_due_date = due_date
            last_schedule_amount = amount

        if schedule_total != invoice_total:
            raise InvalidAgreementError(
                "Custom schedule installment amounts must add up to the invoice total."
            )

        if (
            final_installment_amount is not None
            and final_installment_amount != last_schedule_amount
        ):
            raise InvalidAgreementError(
                "Final installment amount must match the last custom schedule entry."
            )

    elif custom_schedule is not None:
        raise InvalidAgreementError(
            "Only custom payment plans can include a custom schedule."
        )

def create_payment_agreement(
    *,
    public_job_id,
    contractor_pk,
    account_id,
    arrangement_type,
    invoice_total,
    schedule_type=None,
    installment_amount=None,
    final_installment_amount=None,
    plan_start_date=None,
    final_due_date=None,
    custom_schedule=None,
):
    _validate_terms(
        arrangement_type,
        invoice_total,
        schedule_type,
        installment_amount,
        final_installment_amount,
        plan_start_date,
        final_due_date,
        custom_schedule,
    )

    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT id, lead_id
                FROM jobs
                WHERE job_id = %s AND contractor_id = %s
                """,
                (public_job_id, contractor_pk),
            )
            initial_job = cursor.fetchone()
            if initial_job is None:
                raise AgreementJobNotFoundError()

            # Match the existing job-status service lock order.
            cursor.execute(
                "SELECT id FROM leads WHERE id = %s FOR UPDATE",
                (initial_job["lead_id"],),
            )
            cursor.execute(
                """
                SELECT id, lead_id, job_id
                FROM jobs
                WHERE id = %s AND contractor_id = %s
                FOR UPDATE
                """,
                (initial_job["id"], contractor_pk),
            )
            job = cursor.fetchone()
            if job is None:
                raise AgreementJobNotFoundError()

            cursor.execute(
                """
                SELECT id, version
                FROM job_payment_agreements
                WHERE job_id = %s AND contractor_id = %s
                ORDER BY version DESC
                LIMIT 1
                FOR UPDATE
                """,
                (job["id"], contractor_pk),
            )
            previous = cursor.fetchone()
            version = previous["version"] + 1 if previous else 1
            supersedes_id = previous["id"] if previous else None

            cursor.execute(
                """
                INSERT INTO job_payment_agreements (
                    job_id, contractor_id, version, arrangement_type,
                    invoice_total, schedule_type, installment_amount,
                    final_installment_amount, plan_start_date, final_due_date,
                    custom_schedule, created_by_account_id, supersedes_agreement_id
                )
                VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                )
                RETURNING *
                """,
                (
                    job["id"],
                    contractor_pk,
                    version,
                    arrangement_type,
                    invoice_total,
                    schedule_type,
                    installment_amount,
                    final_installment_amount,
                    plan_start_date,
                    final_due_date,
                    Jsonb(custom_schedule) if custom_schedule is not None else None,
                    account_id,
                    supersedes_id,
                ),
            )
            agreement = _agreement_dict(cursor.fetchone())

            cursor.execute(
                """
                INSERT INTO job_financial_audit_events (
                    job_id, contractor_id, actor_type, actor_account_id,
                    event_type, related_record_type, related_record_id, details
                )
                VALUES (
                    %s, %s, 'CONTRACTOR', %s,
                    'PAYMENT_AGREEMENT_CREATED',
                    'job_payment_agreements', %s, %s
                )
                """,
                (
                    job["id"],
                    contractor_pk,
                    account_id,
                    agreement["id"],
                    Jsonb({
                        "version": version,
                        "arrangement_type": arrangement_type,
                        "invoice_total": str(invoice_total),
                        "supersedes_agreement_id": supersedes_id,
                    }),
                ),
            )

            return {
                "job_id": job["job_id"],
                "agreement": agreement,
                "supersedes_agreement_id": supersedes_id,
            }


def get_payment_agreement(*, public_job_id, contractor_pk):
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT id, lead_id
                FROM jobs
                WHERE job_id = %s AND contractor_id = %s
                """,
                (public_job_id, contractor_pk),
            )
            initial_job = cursor.fetchone()
            if initial_job is None:
                raise AgreementJobNotFoundError()

            cursor.execute(
                "SELECT id FROM leads WHERE id = %s",
                (initial_job["lead_id"],),
            )
            if cursor.fetchone() is None:
                raise AgreementJobNotFoundError()

            cursor.execute(
                """
                SELECT *
                FROM job_payment_agreements
                WHERE job_id = %s AND contractor_id = %s
                ORDER BY version DESC
                LIMIT 1
                """,
                (initial_job["id"], contractor_pk),
            )
            agreement = _agreement_dict(cursor.fetchone())

            cursor.execute(
                """
                SELECT
                    COALESCE(SUM(c.amount), 0) AS collected_total,
                    COALESCE(SUM(a.fee_amount), 0) AS fee_accrued
                FROM job_collections c
                LEFT JOIN job_fee_accruals a ON a.collection_id = c.id
                WHERE c.job_id = %s
                  AND c.contractor_id = %s
                  AND c.report_status = 'ACTIVE'
                """,
                (initial_job["id"], contractor_pk),
            )
            totals = cursor.fetchone()
            collected_total = _money(totals["collected_total"])
            fee_accrued = _money(totals["fee_accrued"])

            invoice_total = (
                Decimal(agreement["invoice_total"])
                if agreement is not None
                else None
            )

            return {
                "job_id": public_job_id,
                "agreement": agreement,
                "payment_summary": {
                    "collected_total": str(collected_total),
                    "remaining_balance": (
                        str(max(Decimal("0.00"), invoice_total - collected_total))
                        if invoice_total is not None
                        else None
                    ),
                    "fee_accrued": str(fee_accrued),
                },
            }
