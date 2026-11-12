import calendar

from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP, ROUND_DOWN
from typing import Any

from psycopg.types.json import Jsonb

from app.database.connection import get_connection
from app.services.collections import record_collection
CENT = Decimal("0.01")


def _money(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)

def _add_months(anchor: date, months: int) -> date:
    """Add calendar months while preserving the original day when possible."""
    month_index = anchor.month - 1 + months
    year = anchor.year + month_index // 12
    month = month_index % 12 + 1

    day = min(
        anchor.day,
        calendar.monthrange(year, month)[1],
    )

    return date(year, month, day)


def calculate_installment_schedule(
    *,
    invoice_total: Decimal,
    down_payment_amount: Decimal,
    schedule_type: str,
    term_months: int,
    plan_start_date: date,
) -> dict[str, Any]:
    """
    Calculate a payment schedule from the invoice total, down payment,
    frequency, agreement length, and plan start date.

    The down payment, when present, is due on the plan start date.
    Recurring payments always begin one full interval afterward.
    """

    invoice_total = _money(invoice_total)
    down_payment_amount = _money(down_payment_amount)

    if invoice_total <= 0:
        raise InvalidAgreementError("Invoice total must be greater than zero.")

    if down_payment_amount < 0 or down_payment_amount >= invoice_total:
        raise InvalidAgreementError(
            "Down payment must be zero or greater and less than the invoice total."
        )

    if not isinstance(term_months, int) or term_months < 1:
        raise InvalidAgreementError(
            "Agreement length must be at least one month."
        )

    frequency = schedule_type.upper()

    if frequency not in {"WEEKLY", "BIWEEKLY", "MONTHLY"}:
        raise InvalidAgreementError("Invalid payment frequency.")

    term_end_date = _add_months(plan_start_date, term_months)

    due_dates: list[date] = []
    interval = 1

    while True:
        if frequency == "WEEKLY":
            due_date = plan_start_date + timedelta(days=7 * interval)
        elif frequency == "BIWEEKLY":
            due_date = plan_start_date + timedelta(days=14 * interval)
        else:
            due_date = _add_months(plan_start_date, interval)

        if due_date > term_end_date:
            break

        due_dates.append(due_date)
        interval += 1

    if not due_dates:
        raise InvalidAgreementError(
            "The selected agreement length does not produce any installments."
        )

    remaining_balance = _money(invoice_total - down_payment_amount)

    # Round recurring payments down to cents. The final payment receives
    # the remaining balance, absorbing any rounding difference.
    regular_amount = (
        remaining_balance / Decimal(len(due_dates))
    ).quantize(Decimal("0.01"), rounding=ROUND_DOWN)

    installments: list[dict[str, Any]] = []

    if down_payment_amount > 0:
        installments.append({
            "installment_number": 1,
            "installment_type": "DOWN_PAYMENT",
            "principal_amount": down_payment_amount,
            "due_date": plan_start_date,
        })

    remaining_after_regular = remaining_balance

    for index, due_date in enumerate(due_dates):
        is_final = index == len(due_dates) - 1

        amount = (
            remaining_after_regular
            if is_final
            else regular_amount
        )

        amount = _money(amount)
        remaining_after_regular = _money(
            remaining_after_regular - amount
        )

        installments.append({
            "installment_number": len(installments) + 1,
            "installment_type": "FINAL" if is_final else "REGULAR",
            "principal_amount": amount,
            "due_date": due_date,
        })

    if remaining_after_regular != Decimal("0.00"):
        raise InvalidAgreementError(
            "The calculated installment schedule does not balance."
        )

    return {
        "installment_count": len(due_dates),
        "installment_amount": regular_amount,
        "final_installment_amount": installments[-1]["principal_amount"],
        "final_due_date": due_dates[-1],
        "installments": installments,
    }

def _create_installment_records(
    cursor,
    *,
    agreement_id: int,
    job_id: int,
    contractor_id: int,
    installments: list[dict[str, Any]],
) -> None:
    """Persist the calculated installments for an operative agreement."""

    for installment in installments:
        cursor.execute(
            """
            INSERT INTO job_installments (
                agreement_id,
                job_id,
                contractor_id,
                installment_number,
                principal_amount,
                due_date,
                status,
                installment_type
            )
            VALUES (%s, %s, %s, %s, %s, %s, 'SCHEDULED', %s)
            """,
            (
                agreement_id,
                job_id,
                contractor_id,
                installment["installment_number"],
                installment["principal_amount"],
                installment["due_date"],
                installment["installment_type"],
            ),
        )

def _installments_from_saved_agreement(
    agreement: dict,
    *,
    collected_total: Decimal = Decimal("0.00"),
) -> list[dict[str, Any]]:
    """Reconstruct the outstanding installments from saved agreement terms."""

    if agreement["arrangement_type"] != "PAYMENT_PLAN":
        return []

    start_date = agreement["plan_start_date"]
    if isinstance(start_date, str):
        start_date = date.fromisoformat(start_date)

    final_due_date = agreement["final_due_date"]
    if isinstance(final_due_date, str):
        final_due_date = date.fromisoformat(final_due_date)

    down_payment = _money(
        Decimal(str(agreement.get("down_payment_amount") or "0.00"))
    )

    installments = []

    if down_payment > 0:
        installments.append({
            "installment_number": 1,
            "installment_type": "DOWN_PAYMENT",
            "principal_amount": down_payment,
            "due_date": start_date,
        })

    frequency = agreement["schedule_type"]

    if frequency == "CUSTOM":
        custom_schedule = agreement.get("custom_schedule")

        if not isinstance(custom_schedule, list) or not custom_schedule:
            raise InvalidAgreementError(
                "The saved custom payment schedule is invalid."
            )

        for index, item in enumerate(custom_schedule):
            due_date = item["due_date"]
            if isinstance(due_date, str):
                due_date = date.fromisoformat(due_date)

            amount = _money(Decimal(str(item["amount"])))
            is_final = index == len(custom_schedule) - 1

            installments.append({
                "installment_number": len(installments) + 1,
                "installment_type": "FINAL" if is_final else "REGULAR",
                "principal_amount": amount,
                "due_date": due_date,
            })

    else:
        count = agreement["installment_count"]
        regular_amount = _money(
            Decimal(str(agreement["installment_amount"]))
        )

        final_value = agreement.get("final_installment_amount")
        final_amount = (
            _money(Decimal(str(final_value)))
            if final_value is not None
            else regular_amount
        )

        for index in range(1, count + 1):
            if frequency == "WEEKLY":
                due_date = start_date + timedelta(days=7 * index)
            elif frequency == "BIWEEKLY":
                due_date = start_date + timedelta(days=14 * index)
            elif frequency == "MONTHLY":
                due_date = _add_months(start_date, index)
            else:
                raise InvalidAgreementError(
                    "The saved payment frequency is not supported."
                )

            is_final = index == count

            installments.append({
                "installment_number": len(installments) + 1,
                "installment_type": "FINAL" if is_final else "REGULAR",
                "principal_amount": (
                    final_amount if is_final else regular_amount
                ),
                "due_date": due_date,
            })

    invoice_total = _money(Decimal(str(agreement["invoice_total"])))
    collected_total = _money(Decimal(str(collected_total)))

    if collected_total < 0 or collected_total > invoice_total:
        raise InvalidAgreementError(
            "Previously collected payments exceed the revised invoice total."
        )

    # Apply historical collections to the earliest installments first.
    remaining_collected = collected_total
    outstanding_installments = []

    for installment in installments:
        amount = installment["principal_amount"]
        applied = min(remaining_collected, amount)
        outstanding_amount = _money(amount - applied)
        remaining_collected = _money(remaining_collected - applied)

        if outstanding_amount > 0:
            outstanding_installments.append({
                **installment,
                "installment_number": len(outstanding_installments) + 1,
                "principal_amount": outstanding_amount,
            })

    expected_outstanding = _money(invoice_total - collected_total)
    actual_outstanding = _money(sum(
        (
            item["principal_amount"]
            for item in outstanding_installments
        ),
        Decimal("0.00"),
    ))

    if actual_outstanding != expected_outstanding:
        raise InvalidAgreementError(
            "The saved installment schedule does not equal the outstanding invoice balance."
        )

    if outstanding_installments:
        if outstanding_installments[-1]["due_date"] != final_due_date:
            raise InvalidAgreementError(
                "The saved installment schedule does not match its final due date."
            )

    return outstanding_installments

class AgreementJobNotFoundError(Exception):
    pass


class InvalidAgreementError(ValueError):
    pass

class AgreementConflictError(Exception):
    pass

def _agreement_dict(row):
    if row is None:
        return None

    result = dict(row)
    for field in (
        "invoice_total",
        "down_payment_amount",
        "installment_amount",
        "final_installment_amount",
    ):
        if result.get(field) is not None:
            result[field] = str(result[field])

    for field in (
        "down_payment_due_date",
        "plan_start_date",
        "final_due_date",
        "paid_in_full_at",
        "accepted_at",
        "admin_revised_at",
        "emg_review_requested_at",
        "created_at",
    ):
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
    down_payment_amount,
    down_payment_due_date,
    installment_count,
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
            down_payment_amount,
            down_payment_due_date,
            installment_count,
        )):
            raise InvalidAgreementError(
                "Paid-in-full agreements cannot include payment-plan terms."
            )
        return

    if arrangement_type != "PAYMENT_PLAN":
        raise InvalidAgreementError("Unsupported payment arrangement.")

    if (
        not isinstance(down_payment_amount, Decimal)
        or not down_payment_amount.is_finite()
        or down_payment_amount < 0
        or down_payment_amount != _money(down_payment_amount)
    ):
        raise InvalidAgreementError(
            "Down payment must be zero or a positive amount with no more than two decimal places."
        )

    if down_payment_amount >= invoice_total:
        raise InvalidAgreementError(
            "Down payment must be less than the total invoice amount."
        )

    if down_payment_due_date is None:
        raise InvalidAgreementError(
            "Payment plans require a down-payment due date."
        )

    if not isinstance(installment_count, int) or isinstance(installment_count, bool) or installment_count <= 0:
        raise InvalidAgreementError(
            "Payment plans require a positive installment count."
        )

    remaining_balance = _money(invoice_total - down_payment_amount)

    if schedule_type not in ("WEEKLY", "BIWEEKLY", "MONTHLY", "CUSTOM"):
        raise InvalidAgreementError("Choose a valid payment schedule.")

    if not isinstance(installment_amount, Decimal) or not installment_amount.is_finite():
        raise InvalidAgreementError("Installment amount must be a valid amount.")

    if installment_amount <= 0 or installment_amount != _money(installment_amount):
        raise InvalidAgreementError(
            "Installment amount must be positive and have no more than two decimal places."
        )

    if installment_amount > remaining_balance:
        raise InvalidAgreementError(
            "Installment amount cannot exceed the remaining balance."
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

    if plan_start_date is None or final_due_date is None:
        raise InvalidAgreementError(
            "Payment plans require a start date and final due date."
        )

    if final_due_date < plan_start_date:
        raise InvalidAgreementError(
            "Final due date cannot be before the plan start date."
        )

    if down_payment_due_date > plan_start_date:
        raise InvalidAgreementError(
            "Down payment cannot be due after the first scheduled installment."
        )

    if schedule_type == "CUSTOM":
        if not isinstance(custom_schedule, list) or not custom_schedule:
            raise InvalidAgreementError(
                "Custom schedules require at least one schedule entry."
            )

        if len(custom_schedule) != installment_count:
            raise InvalidAgreementError(
                "Installment count must match the number of custom schedule entries."
            )

        schedule_total = Decimal("0.00")
        previous_due_date = None
        last_schedule_amount = None

        for item in custom_schedule:
            if (
                not isinstance(item, dict)
                or not isinstance(item.get("due_date"), str)
                or item.get("amount") is None
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

        if schedule_total != remaining_balance:
            raise InvalidAgreementError(
                "Custom schedule installment amounts must add up to the invoice balance after the down payment."
            )

        if (
            final_installment_amount is not None
            and final_installment_amount != last_schedule_amount
        ):
            raise InvalidAgreementError(
                "Final installment amount must match the last custom schedule entry."
            )

    else:
        if custom_schedule is not None:
            raise InvalidAgreementError(
                "Only custom payment plans can include a custom schedule."
            )

        scheduled_total = (
            installment_amount * (installment_count - 1)
            + (
                final_installment_amount
                if final_installment_amount is not None
                else installment_amount
            )
        )

        if scheduled_total != remaining_balance:
            raise InvalidAgreementError(
                "Installment amounts and count must add up to the invoice balance after the down payment."
            )

def create_payment_agreement(
    *,
    public_job_id,
    contractor_pk,
    account_id,
    arrangement_type,
    invoice_total,
    down_payment_amount=None,
    down_payment_due_date=None,
    installment_count=None,
    schedule_type=None,
    term_months=None,
    installment_amount=None,
    final_installment_amount=None,
    plan_start_date=None,
    final_due_date=None,
    custom_schedule=None,
):
    if arrangement_type == "PAYMENT_PLAN":
        if plan_start_date is None:
            raise InvalidAgreementError(
                "A plan start date is required."
            )

        down_payment_amount = _money(
            down_payment_amount or Decimal("0.00")
        )

        if term_months is not None:
            if schedule_type == "CUSTOM":
                raise InvalidAgreementError(
                    "Custom payment plans cannot specify an agreement length."
                )

            schedule = calculate_installment_schedule(
                invoice_total=invoice_total,
                down_payment_amount=down_payment_amount,
                schedule_type=schedule_type,
                term_months=term_months,
                plan_start_date=plan_start_date,
            )

            down_payment_due_date = plan_start_date
            installment_count = schedule["installment_count"]
            installment_amount = schedule["installment_amount"]
            final_installment_amount = schedule["final_installment_amount"]
            final_due_date = schedule["final_due_date"]
        else:
            down_payment_due_date = (
                down_payment_due_date or plan_start_date
            )

    _validate_terms(
        arrangement_type,
        invoice_total,
        schedule_type,
        installment_amount,
        final_installment_amount,
        plan_start_date,
        final_due_date,
        custom_schedule,
        down_payment_amount,
        down_payment_due_date,
        installment_count,
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
                SELECT id, version, status
                FROM job_payment_agreements
                WHERE job_id = %s AND contractor_id = %s
                ORDER BY version DESC
                LIMIT 1
                FOR UPDATE
                """,
                (job["id"], contractor_pk),
            )
            previous = cursor.fetchone()
            if previous is not None:
                if previous["status"] == "ACCEPTED":
                    raise AgreementConflictError(
                        "An accepted payment agreement cannot be replaced by the contractor."
                    )

                if previous["status"] != "PENDING_ACCEPTANCE":
                    raise AgreementConflictError(
                        "Only a payment agreement awaiting contractor acceptance can be replaced."
                    )

                cursor.execute(
                    """
                    UPDATE job_payment_agreements
                    SET status = 'SUPERSEDED'
                    WHERE id = %s
                      AND status = %s
                    """,
                    (previous["id"], previous["status"]),
                )

                if cursor.rowcount != 1:
                    raise AgreementConflictError(
                        "The previous payment agreement could not be superseded."
                    )
            version = previous["version"] + 1 if previous else 1
            supersedes_id = previous["id"] if previous else None

            cursor.execute(
                """
                INSERT INTO job_payment_agreements (
                job_id, contractor_id, version, arrangement_type,
                invoice_total, down_payment_amount, down_payment_due_date,
                installment_count, schedule_type, installment_amount,
                final_installment_amount, plan_start_date, final_due_date,
                custom_schedule, created_by_account_id, supersedes_agreement_id
                )
                VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s
                )
                RETURNING *
                """,
                (
                    job["id"],
                    contractor_pk,
                    version,
                    arrangement_type,
                    invoice_total,
                    down_payment_amount,
                    down_payment_due_date,
                    installment_count,
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
            if previous is not None:
                cursor.execute(
                    """
                    INSERT INTO job_financial_audit_events (
                        job_id, contractor_id, actor_type, actor_account_id,
                        event_type, related_record_type, related_record_id, details
                    )
                    VALUES (
                        %s, %s, 'CONTRACTOR', %s,
                        'PAYMENT_AGREEMENT_SUPERSEDED',
                        'job_payment_agreements', %s, %s
                    )
                    """,
                    (
                        job["id"],
                        contractor_pk,
                        account_id,
                        previous["id"],
                        Jsonb({
                            "superseded_agreement_id": previous["id"],
                            "replacement_agreement_id": agreement["id"],
                            "replacement_version": version,
                        }),
                    ),
                )

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


def revise_payment_agreement(
    *,
    public_job_id,
    admin_id,
    admin_username,
    reason,
    arrangement_type,
    invoice_total,
    term_months=None,
    down_payment_amount=None,
    down_payment_due_date=None,
    installment_count=None,
    schedule_type=None,
    installment_amount=None,
    final_installment_amount=None,
    plan_start_date=None,
    final_due_date=None,
    custom_schedule=None,
):
    reason = reason.strip() if isinstance(reason, str) else ""

    if not reason:
        raise InvalidAgreementError(
            "An explanation is required for an administrator agreement revision."
        )

    if term_months is not None:
        if arrangement_type != "PAYMENT_PLAN":
            raise InvalidAgreementError(
                "Agreement length can only be provided for a payment plan."
            )

        if schedule_type not in {"WEEKLY", "BIWEEKLY", "MONTHLY"}:
            raise InvalidAgreementError(
                "Agreement length is only supported for weekly, "
                "biweekly, or monthly payment plans."
            )

        if plan_start_date is None:
            raise InvalidAgreementError(
                "A plan start date is required."
            )

        down_payment_amount = _money(
            down_payment_amount or Decimal("0.00")
        )

        calculated = calculate_installment_schedule(
            invoice_total=invoice_total,
            down_payment_amount=down_payment_amount,
            schedule_type=schedule_type,
            term_months=term_months,
            plan_start_date=plan_start_date,
        )

        down_payment_due_date = plan_start_date
        installment_count = calculated["installment_count"]
        installment_amount = calculated["installment_amount"]
        final_installment_amount = calculated[
            "final_installment_amount"
        ]
        final_due_date = calculated["final_due_date"]
        custom_schedule = None

    _validate_terms(
        arrangement_type,
        invoice_total,
        schedule_type,
        installment_amount,
        final_installment_amount,
        plan_start_date,
        final_due_date,
        custom_schedule,
        down_payment_amount,
        down_payment_due_date,
        installment_count,
    )

    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT id, lead_id, contractor_id, job_id
                FROM jobs
                WHERE job_id = %s
                """,
                (public_job_id,),
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
                SELECT id, lead_id, contractor_id, job_id
                FROM jobs
                WHERE id = %s
                FOR UPDATE
                """,
                (initial_job["id"],),
            )
            job = cursor.fetchone()

            if job is None:
                raise AgreementJobNotFoundError()

            cursor.execute(
                """
                SELECT id, version, status
                FROM job_payment_agreements
                WHERE job_id = %s AND contractor_id = %s
                ORDER BY version DESC
                LIMIT 1
                FOR UPDATE
                """,
                (job["id"], job["contractor_id"]),
            )
            previous = cursor.fetchone()

            if previous is None:
                raise AgreementConflictError(
                    "There is no existing payment agreement to revise."
                )

            if previous["status"] not in (
                "PENDING_ACCEPTANCE",
                "PENDING_EMG_REVIEW",
                "ACCEPTED",
                "ADMIN_REVISED",
            ):
                raise AgreementConflictError(
                    "The payment agreement cannot be revised in its current state."
                )
            # Preserve all active collections as payments against
            # the revised agreement.
            cursor.execute(
                """
                SELECT COALESCE(SUM(amount), 0) AS collected_total
                FROM job_collections
                WHERE job_id = %s
                  AND report_status = 'ACTIVE'
                """,
                (job["id"],),
            )
            collection_row = cursor.fetchone()
            collected_total = _money(
                Decimal(str(collection_row["collected_total"]))
            )

            revised_invoice_total = _money(Decimal(str(invoice_total)))

            if collected_total > revised_invoice_total:
                raise InvalidAgreementError(
                    "The revised invoice total cannot be less than "
                    "the amount already collected."
                )
            cursor.execute(
                """
                UPDATE job_payment_agreements
                SET status = 'SUPERSEDED'
                WHERE id = %s
                  AND status = %s
                """,
                (previous["id"], previous["status"]),
            )

            if cursor.rowcount != 1:
                raise AgreementConflictError(
                    "The previous payment agreement could not be superseded."
                )
            if previous["status"] in ("ACCEPTED", "ADMIN_REVISED"):
                cursor.execute(
                    """
                    UPDATE job_installments
                    SET status = 'CANCELLED'
                    WHERE agreement_id = %s
                      AND status = 'SCHEDULED'
                    """,
                    (previous["id"],),
                )

            version = previous["version"] + 1

            cursor.execute(
                """
                INSERT INTO job_payment_agreements (
                    job_id,
                    contractor_id,
                    version,
                    arrangement_type,
                    invoice_total,
                    down_payment_amount,
                    down_payment_due_date,
                    installment_count,
                    schedule_type,
                    installment_amount,
                    final_installment_amount,
                    plan_start_date,
                    final_due_date,
                    custom_schedule,
                    status,
                    created_by_account_id,
                    created_by_admin_user_id,
                    admin_revision_reason,
                    admin_revised_at,
                    supersedes_agreement_id
                )
                VALUES (
                    %s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, 'ADMIN_REVISED',
                    NULL, %s, %s, CURRENT_TIMESTAMP, %s
                )
                RETURNING *
                """,
                (
                    job["id"],
                    job["contractor_id"],
                    version,
                    arrangement_type,
                    invoice_total,
                    down_payment_amount,
                    down_payment_due_date,
                    installment_count,
                    schedule_type,
                    installment_amount,
                    final_installment_amount,
                    plan_start_date,
                    final_due_date,
                    Jsonb(custom_schedule)
                    if custom_schedule is not None
                    else None,
                    admin_id,
                    reason,
                    previous["id"],
                ),
            )

            agreement = _agreement_dict(cursor.fetchone())
            if arrangement_type == "PAYMENT_PLAN":
                installments = _installments_from_saved_agreement(
                    agreement,
                    collected_total=collected_total,
                )

                _create_installment_records(
                    cursor,
                    agreement_id=agreement["id"],
                    job_id=job["id"],
                    contractor_id=job["contractor_id"],
                    installments=installments,
                )

            cursor.execute(
                """
                INSERT INTO job_financial_audit_events (
                    job_id,
                    contractor_id,
                    actor_type,
                    actor_account_id,
                    event_type,
                    related_record_type,
                    related_record_id,
                    details
                )
                VALUES (
                    %s, %s, 'EMG', NULL,
                    'PAYMENT_AGREEMENT_ADMIN_REVISED',
                    'job_payment_agreements', %s, %s
                )
                """,
                (
                    job["id"],
                    job["contractor_id"],
                    agreement["id"],
                    Jsonb({
                        "admin_id": admin_id,
                        "admin_username": admin_username,
                        "reason": reason,
                        "previous_agreement_id": previous["id"],
                        "new_agreement_id": agreement["id"],
                        "version": version,
                        "arrangement_type": arrangement_type,
                        "invoice_total": str(invoice_total),
                    }),
                ),
            )

            return {
                "job_id": job["job_id"],
                "agreement": agreement,
                "supersedes_agreement_id": previous["id"],
            }

def accept_payment_agreement(
    *,
    public_job_id,
    contractor_pk,
    account_id,
):
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

            # Preserve the established lead-then-job lock order.
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
                SELECT *
                FROM job_payment_agreements
                WHERE job_id = %s
                  AND contractor_id = %s
                ORDER BY version DESC
                LIMIT 1
                FOR UPDATE
                """,
                (job["id"], contractor_pk),
            )
            agreement_row = cursor.fetchone()

            if agreement_row is None:
                raise AgreementConflictError(
                    "There is no payment agreement to accept."
                )

            is_paid_in_full_retry = (
                agreement_row["status"] == "ACCEPTED"
                and agreement_row["arrangement_type"] == "PAID_IN_FULL"
            )

            is_payment_plan_retry = (
                agreement_row["status"] == "ACCEPTED"
                and agreement_row["arrangement_type"] == "PAYMENT_PLAN"
            )

            if (
                agreement_row["status"] != "PENDING_ACCEPTANCE"
                and not is_paid_in_full_retry
                and not is_payment_plan_retry
            ):
                raise AgreementConflictError(
                    "Only a pending payment agreement can be accepted."
                )

            agreement = _agreement_dict(agreement_row)
            installments = _installments_from_saved_agreement(agreement)

            collection_result = None

            if is_paid_in_full_retry or is_payment_plan_retry:
                # Recover an already-accepted agreement without
                # creating duplicate installments or audit events.
                accepted_agreement = agreement

            else:
                cursor.execute(
                    """
                    UPDATE job_payment_agreements
                    SET status = 'ACCEPTED',
                        accepted_at = CURRENT_TIMESTAMP,
                        accepted_by_account_id = %s
                    WHERE id = %s
                      AND status = 'PENDING_ACCEPTANCE'
                    RETURNING *
                    """,
                    (account_id, agreement["id"]),
                )

                accepted_row = cursor.fetchone()

                if accepted_row is None:
                    raise AgreementConflictError(
                        "The payment agreement could not be accepted."
                    )

                if installments:
                    _create_installment_records(
                        cursor,
                        agreement_id=agreement["id"],
                        job_id=job["id"],
                        contractor_id=contractor_pk,
                        installments=installments,
                    )

                accepted_agreement = _agreement_dict(accepted_row)

                cursor.execute(
                    """
                    INSERT INTO job_financial_audit_events (
                        job_id,
                        contractor_id,
                        actor_type,
                        actor_account_id,
                        event_type,
                        related_record_type,
                        related_record_id,
                        details
                    )
                    VALUES (
                        %s, %s, 'CONTRACTOR', %s,
                        'PAYMENT_AGREEMENT_ACCEPTED',
                        'job_payment_agreements', %s, %s
                    )
                    """,
                    (
                        job["id"],
                        contractor_pk,
                        account_id,
                        agreement["id"],
                        Jsonb({
                            "agreement_id": agreement["id"],
                            "version": agreement["version"],
                            "arrangement_type": agreement["arrangement_type"],
                            "invoice_total": agreement["invoice_total"],
                            "installment_count": len(installments),
                        }),
                    ),
                )

            if agreement["arrangement_type"] == "PAID_IN_FULL":
                collection_result = record_collection(
                    public_job_id=public_job_id,
                    contractor_pk=contractor_pk,
                    account_id=account_id,
                    paid_in_full_agreement_id=agreement["id"],
                    db_connection=connection,
                )

            elif (
                agreement["arrangement_type"] == "PAYMENT_PLAN"
                and agreement["down_payment_amount"] is not None
                and Decimal(str(agreement["down_payment_amount"])) > 0
            ):
                down_payment_amount = Decimal(
                    str(agreement["down_payment_amount"])
                )
                accepted_at = datetime.fromisoformat(
                    accepted_agreement["accepted_at"]
                )
                collection_result = record_collection(
                    public_job_id=public_job_id,
                    contractor_pk=contractor_pk,
                    account_id=account_id,
                    amount=down_payment_amount,
                    collected_at=accepted_at,
                    idempotency_key=(
                        f"payment-agreement-down-payment:{agreement['id']}"
                    ),
                    db_connection=connection,
                )

                cursor.execute(
                    """
                    UPDATE job_installments
                    SET status = 'COLLECTED_CONFIRMED'
                    WHERE agreement_id = %s
                      AND installment_type = 'DOWN_PAYMENT'
                      AND status = 'SCHEDULED'
                    """,
                    (agreement["id"],),
                )

            result = {
                "job_id": job["job_id"],
                "agreement": accepted_agreement,
            }

            if collection_result is not None:
                result["collection"] = collection_result

            return result

def request_emg_payment_agreement_review(
    *,
    public_job_id,
    contractor_pk,
    account_id,
    note=None,
):
    if note is not None:
        note = note.strip() or None

        if note is not None and len(note) > 2000:
            raise InvalidAgreementError(
                "The note cannot exceed 2,000 characters."
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

            # Preserve the established lead-then-job lock order.
            cursor.execute(
                "SELECT id FROM leads WHERE id = %s FOR UPDATE",
                (initial_job["lead_id"],),
            )

            cursor.execute(
                """
                SELECT id, lead_id, job_id, contractor_id
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
                SELECT *
                FROM job_payment_agreements
                WHERE job_id = %s
                  AND contractor_id = %s
                ORDER BY version DESC
                LIMIT 1
                FOR UPDATE
                """,
                (job["id"], contractor_pk),
            )
            agreement_row = cursor.fetchone()

            if agreement_row is None:
                raise AgreementConflictError(
                    "There is no payment agreement to submit for EMG review."
                )

            if agreement_row["status"] != "PENDING_ACCEPTANCE":
                raise AgreementConflictError(
                    "Only a pending payment agreement can be submitted for EMG review."
                )

            cursor.execute(
                """
                UPDATE job_payment_agreements
                SET status = 'PENDING_EMG_REVIEW',
                    emg_review_requested_at = CURRENT_TIMESTAMP,
                    emg_review_request_note = %s
                WHERE id = %s
                  AND status = 'PENDING_ACCEPTANCE'
                RETURNING *
                """,
                (note, agreement_row["id"]),
            )

            updated_row = cursor.fetchone()

            if updated_row is None:
                raise AgreementConflictError(
                    "The payment agreement could not be submitted for EMG review."
                )

            agreement = _agreement_dict(updated_row)

            cursor.execute(
                """
                INSERT INTO job_financial_audit_events (
                    job_id,
                    contractor_id,
                    actor_type,
                    actor_account_id,
                    event_type,
                    related_record_type,
                    related_record_id,
                    details
                )
                VALUES (
                    %s, %s, 'CONTRACTOR', %s,
                    'PAYMENT_AGREEMENT_EMG_REVIEW_REQUESTED',
                    'job_payment_agreements', %s, %s
                )
                """,
                (
                    job["id"],
                    contractor_pk,
                    account_id,
                    agreement["id"],
                    Jsonb({
                        "agreement_id": agreement["id"],
                        "version": agreement["version"],
                        "arrangement_type": agreement["arrangement_type"],
                        "invoice_total": agreement["invoice_total"],
                        "note": note,
                    }),
                ),
            )

            return {
                "job_id": job["job_id"],
                "agreement": agreement,
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
            installments = []
            schedule_error = None

            if (
                agreement is not None
                and agreement["arrangement_type"] == "PAYMENT_PLAN"
            ):
                try:
                    saved_installments = (
                        _installments_from_saved_agreement(agreement)
                    )

                    installments = [
                        {
                            "installment_number": item["installment_number"],
                            "installment_type": item["installment_type"],
                            "principal_amount": str(item["principal_amount"]),
                            "due_date": item["due_date"].isoformat(),
                        }
                        for item in saved_installments
                    ]
                except InvalidAgreementError as exc:
                    schedule_error = str(exc)

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
                "installments": installments,
                "schedule_error": schedule_error,
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

def get_admin_payment_agreement(*, public_job_id):
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT id, contractor_id
                FROM jobs
                WHERE job_id = %s
                """,
                (public_job_id,),
            )
            job = cursor.fetchone()

            if job is None:
                raise AgreementJobNotFoundError()

            cursor.execute(
                """
                SELECT *
                FROM job_payment_agreements
                WHERE job_id = %s
                  AND contractor_id = %s
                ORDER BY version DESC
                """,
                (job["id"], job["contractor_id"]),
            )
            agreement_rows = cursor.fetchall()

            agreement_history = []

            for row in agreement_rows:
                version_agreement = _agreement_dict(row)
                version_installments = []
                schedule_error = None

                if version_agreement["arrangement_type"] == "PAYMENT_PLAN":
                    try:
                        saved_installments = (
                            _installments_from_saved_agreement(
                                version_agreement
                            )
                        )

                        version_installments = [
                            {
                                "installment_number": item["installment_number"],
                                "installment_type": item["installment_type"],
                                "principal_amount": str(item["principal_amount"]),
                                "due_date": item["due_date"].isoformat(),
                            }
                            for item in saved_installments
                        ]
                    except InvalidAgreementError as exc:
                        schedule_error = str(exc)

                agreement_history.append(
                    {
                        "agreement": version_agreement,
                        "installments": version_installments,
                        "schedule_error": schedule_error,
                    }
                )

            agreement = (
                agreement_history[0]["agreement"]
                if agreement_history
                else None
            )

            cursor.execute(
                """
                SELECT
                    COALESCE(SUM(c.amount), 0) AS collected_total,
                    COALESCE(SUM(a.fee_amount), 0) AS fee_accrued
                FROM job_collections c
                LEFT JOIN job_fee_accruals a
                  ON a.collection_id = c.id
                WHERE c.job_id = %s
                  AND c.contractor_id = %s
                  AND c.report_status = 'ACTIVE'
                """,
                (job["id"], job["contractor_id"]),
            )
            totals = cursor.fetchone()

            collected_total = _money(totals["collected_total"])
            fee_accrued = _money(totals["fee_accrued"])

            invoice_total = (
                Decimal(agreement["invoice_total"])
                if agreement is not None
                else None
            )

            installments = (
                agreement_history[0]["installments"]
                if agreement_history
                else []
            )

            return {
                "job_id": public_job_id,
                "agreement": agreement,
                "agreement_history": agreement_history,
                "installments": installments,
                "payment_summary": {
                    "collected_total": str(collected_total),
                    "remaining_balance": (
                        str(
                            max(
                                Decimal("0.00"),
                                invoice_total - collected_total,
                            )
                        )
                        if invoice_total is not None
                        else None
                    ),
                    "fee_accrued": str(fee_accrued),
                },
            }