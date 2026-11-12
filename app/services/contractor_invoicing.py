
from calendar import monthrange
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from app.database.connection import get_connection


BILLING_TIMEZONE = ZoneInfo("America/Los_Angeles")
ZERO_AMOUNT = Decimal("0.00")


class InvoiceGenerationConflictError(RuntimeError):
    """Raised when an issued invoice conflicts with unassigned accruals."""


def _billing_date(year: int, month: int, billing_day: int) -> date:
    """Return the billing date, clamping days to the month's last day."""

    last_day = monthrange(year, month)[1]
    return date(year, month, min(billing_day, last_day))


def _next_month(year: int, month: int) -> tuple[int, int]:
    if month == 12:
        return year + 1, 1

    return year, month + 1


def _previous_month(year: int, month: int) -> tuple[int, int]:
    if month == 1:
        return year - 1, 12

    return year, month - 1


def _next_billing_date(
    current: date,
    billing_day: int,
) -> date:
    year, month = _next_month(current.year, current.month)
    return _billing_date(year, month, billing_day)


def _previous_billing_date(
    current: date,
    billing_day: int,
) -> date:
    year, month = _previous_month(current.year, current.month)
    return _billing_date(year, month, billing_day)


def _latest_due_billing_date(
    as_of: date,
    billing_day: int,
) -> date:
    """
    Find the most recent scheduled billing date on or before as_of.

    This permits a later execution to catch up on a missed billing run.
    """

    current_month_date = _billing_date(
        as_of.year,
        as_of.month,
        billing_day,
    )

    if current_month_date <= as_of:
        return current_month_date

    return _previous_billing_date(
        current_month_date,
        billing_day,
    )


def _first_cutoff_after(
    accrued_at: datetime,
    billing_day: int,
) -> date:
    """
    Find the first billing cutoff strictly after an accrual timestamp.

    Accruals occurring at or after midnight on a billing date belong
    to the following billing period.
    """

    if accrued_at.tzinfo is None:
        raise ValueError("Accrual timestamps must be timezone-aware.")

    local_accrual = accrued_at.astimezone(BILLING_TIMEZONE)

    candidate = _billing_date(
        local_accrual.year,
        local_accrual.month,
        billing_day,
    )

    candidate_midnight = datetime.combine(
        candidate,
        time.min,
        tzinfo=BILLING_TIMEZONE,
    )

    if candidate_midnight <= local_accrual:
        candidate = _next_billing_date(candidate, billing_day)

    return candidate


def _eligible_accruals_exist(
    cursor: Any,
    contractor_id: int,
    period_start: datetime,
    period_end: datetime,
) -> bool:
    """Check for unassigned positive fee accruals in a billing period."""

    cursor.execute(
        """
        SELECT EXISTS (
            SELECT 1
            FROM job_fee_accruals a
            WHERE a.contractor_id = %s
              AND a.accrual_status = 'ACCRUED'
              AND a.fee_amount > 0
              AND a.accrued_at >= %s
              AND a.accrued_at < %s

              AND NOT EXISTS (
                  SELECT 1
                  FROM contractor_invoice_obligation_accruals l
                  WHERE l.fee_accrual_id = a.id
              )

              AND NOT EXISTS (
                  SELECT 1
                  FROM contractor_invoice_items li
                  WHERE li.fee_accrual_id = a.id
              )
        ) AS eligible
        """,
        (
            contractor_id,
            period_start,
            period_end,
        ),
    )

    return bool(cursor.fetchone()["eligible"])


def _lock_eligible_accruals(
    cursor: Any,
    contractor_id: int,
    period_start: datetime,
    period_end: datetime,
) -> list[dict[str, Any]]:
    """Lock eligible accruals before assigning them to invoice obligations."""

    cursor.execute(
        """
        SELECT
            a.id,
            a.job_id,
            a.fee_amount,
            a.accrued_at
        FROM job_fee_accruals a
        WHERE a.contractor_id = %s
          AND a.accrual_status = 'ACCRUED'
          AND a.fee_amount > 0
          AND a.accrued_at >= %s
          AND a.accrued_at < %s

          AND NOT EXISTS (
              SELECT 1
              FROM contractor_invoice_obligation_accruals l
              WHERE l.fee_accrual_id = a.id
          )

          AND NOT EXISTS (
              SELECT 1
              FROM contractor_invoice_items li
              WHERE li.fee_accrual_id = a.id
          )

        ORDER BY a.accrued_at, a.job_id, a.id
        FOR UPDATE OF a
        """,
        (
            contractor_id,
            period_start,
            period_end,
        ),
    )

    return cursor.fetchall()


def _get_or_create_draft_invoice(
    cursor: Any,
    contractor_id: int,
    period_start: date,
    period_end: date,
) -> dict[str, Any]:
    """Create a draft invoice if needed, then lock and return it."""

    invoice_number = (
        f"EMG-{contractor_id:06d}-{period_end:%Y%m%d}"
    )

    cursor.execute(
        """
        INSERT INTO contractor_invoices (
            contractor_id,
            billing_period_start,
            billing_period_end,
            invoice_number,
            status,
            total_amount
        )
        VALUES (%s, %s, %s, %s, 'DRAFT', %s)
        ON CONFLICT (
            contractor_id,
            billing_period_start,
            billing_period_end
        )
        DO NOTHING
        """,
        (
            contractor_id,
            period_start,
            period_end,
            invoice_number,
            ZERO_AMOUNT,
        ),
    )

    cursor.execute(
        """
        SELECT
            id,
            contractor_id,
            billing_period_start,
            billing_period_end,
            invoice_number,
            status,
            total_amount
        FROM contractor_invoices
        WHERE contractor_id = %s
          AND billing_period_start = %s
          AND billing_period_end = %s
        FOR UPDATE
        """,
        (
            contractor_id,
            period_start,
            period_end,
        ),
    )

    invoice = cursor.fetchone()

    if invoice is None:
        raise RuntimeError(
            "Invoice could not be created or retrieved."
        )

    return invoice


def _get_or_create_job_obligation(
    cursor: Any,
    invoice_id: int,
    contractor_id: int,
    job_id: int,
) -> int:
    """Return the invoice obligation for a job, creating it if necessary."""

    cursor.execute(
        """
        SELECT id
        FROM contractor_invoice_obligations
        WHERE invoice_id = %s
          AND job_id = %s
        FOR UPDATE
        """,
        (
            invoice_id,
            job_id,
        ),
    )

    obligation = cursor.fetchone()

    if obligation is not None:
        return obligation["id"]

    cursor.execute(
        """
        INSERT INTO contractor_invoice_obligations (
            invoice_id,
            job_id,
            contractor_id
        )
        VALUES (%s, %s, %s)
        RETURNING id
        """,
        (
            invoice_id,
            job_id,
            contractor_id,
        ),
    )

    return cursor.fetchone()["id"]


def _assign_accruals_to_obligations(
    cursor: Any,
    invoice_id: int,
    contractor_id: int,
    accruals: list[dict[str, Any]],
) -> int:
    """
    Group accruals by job and assign them to consolidated obligations.

    Database triggers update obligation balances and accrual statuses.
    """

    accruals_by_job: dict[int, list[dict[str, Any]]] = {}

    for accrual in accruals:
        accruals_by_job.setdefault(
            accrual["job_id"],
            [],
        ).append(accrual)

    for job_id, job_accruals in accruals_by_job.items():
        obligation_id = _get_or_create_job_obligation(
            cursor,
            invoice_id,
            contractor_id,
            job_id,
        )

        for accrual in job_accruals:
            cursor.execute(
                """
                INSERT INTO contractor_invoice_obligation_accruals (
                    obligation_id,
                    fee_accrual_id,
                    job_id,
                    contractor_id
                )
                VALUES (%s, %s, %s, %s)
                """,
                (
                    obligation_id,
                    accrual["id"],
                    job_id,
                    contractor_id,
                ),
            )

    return len(accruals_by_job)


def _prepare_invoice(
    cursor: Any,
    invoice: dict[str, Any],
    contractor_email: str | None,
) -> dict[str, Any]:
    """Prepare the invoice and create its initial delivery record."""

    cursor.execute(
        """
        SELECT
            COUNT(*) AS obligation_count,
            COALESCE(SUM(original_amount), 0) AS total_amount
        FROM contractor_invoice_obligations
        WHERE invoice_id = %s
        """,
        (invoice["id"],),
    )

    totals = cursor.fetchone()
    total_amount = totals["total_amount"]

    if total_amount <= ZERO_AMOUNT:
        raise ValueError(
            "An invoice cannot be prepared with a zero balance."
        )

    cursor.execute(
        """
        UPDATE contractor_invoices
        SET total_amount = %s
        WHERE id = %s
          AND status = 'DRAFT'
        """,
        (
            total_amount,
            invoice["id"],
        ),
    )

    if cursor.rowcount != 1:
        raise InvoiceGenerationConflictError(
            f"Invoice {invoice['id']} could not be prepared."
        )

    recipient_email = (
        contractor_email.strip()
        if contractor_email and contractor_email.strip()
        else None
    )

    delivery_status = "PENDING" if recipient_email else "BLOCKED"

    last_error = (
        None
        if recipient_email
        else "Contractor profile has no usable email address."
    )

    cursor.execute(
        """
        INSERT INTO contractor_invoice_deliveries (
            invoice_id,
            recipient_email,
            delivery_status,
            last_error
        )
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (invoice_id) DO NOTHING
        RETURNING id, delivery_status
        """,
        (
            invoice["id"],
            recipient_email,
            delivery_status,
            last_error,
        ),
    )

    delivery = cursor.fetchone()

    if delivery is not None:
        cursor.execute(
            """
            INSERT INTO contractor_invoice_delivery_events (
                delivery_id,
                event_type,
                details
            )
            VALUES (%s, %s, %s::jsonb)
            """,
            (
                delivery["id"],
                "CREATED" if delivery_status == "PENDING" else "BLOCKED",
                (
                    '{"source": "invoice_preparation", '
                    f'"delivery_status": "{delivery_status}"'
                    '}'
                ),
            ),
        )
    else:
        # An existing delivery record means this invoice was already
        # prepared. Preserve its current delivery state.
        cursor.execute(
            """
            SELECT id, delivery_status
            FROM contractor_invoice_deliveries
            WHERE invoice_id = %s
            """,
            (invoice["id"],),
        )
        delivery = cursor.fetchone()

        if delivery is None:
            raise RuntimeError(
                f"Delivery record for invoice {invoice['id']} "
                "could not be retrieved."
            )

    return {
        "invoice_id": invoice["id"],
        "contractor_id": invoice["contractor_id"],
        "invoice_number": invoice["invoice_number"],
        "billing_period_start": invoice["billing_period_start"],
        "billing_period_end": invoice["billing_period_end"],
        "total_amount": total_amount,
        "obligation_count": totals["obligation_count"],
        "status": "DRAFT",
        "delivery_status": delivery["delivery_status"],
    }


def _process_billing_period(
    cursor: Any,
    contractor_id: int,
    contractor_email: str | None,
    period_start: date,
    period_end: date,
    issued_at: datetime,
) -> dict[str, Any] | None:
    """Prepare one contractor invoice for one billing period."""

    period_start_at = datetime.combine(
        period_start,
        time.min,
        tzinfo=BILLING_TIMEZONE,
    )

    period_end_at = datetime.combine(
        period_end,
        time.min,
        tzinfo=BILLING_TIMEZONE,
    )

    # Lock an existing invoice first, preserving the same lock order
    # used by the obligation-accrual assignment trigger.
    cursor.execute(
        """
        SELECT
            id,
            contractor_id,
            billing_period_start,
            billing_period_end,
            invoice_number,
            status,
            total_amount
        FROM contractor_invoices
        WHERE contractor_id = %s
          AND billing_period_start = %s
          AND billing_period_end = %s
        FOR UPDATE
        """,
        (
            contractor_id,
            period_start,
            period_end,
        ),
    )

    invoice = cursor.fetchone()

    # Lock eligible accruals before deciding whether an invoice is needed.
    accruals = _lock_eligible_accruals(
        cursor,
        contractor_id,
        period_start_at,
        period_end_at,
    )

    # Do not create an empty draft invoice for this period.
    if invoice is None:
        if not accruals:
            return None

        invoice = _get_or_create_draft_invoice(
            cursor,
            contractor_id,
            period_start,
            period_end,
        )

    if invoice["status"] != "DRAFT":
        if accruals:
            raise InvoiceGenerationConflictError(
                f"Invoice {invoice['id']} is already issued, "
                "but eligible unassigned accruals remain in its period."
            )

        return None

    if accruals:
        _assign_accruals_to_obligations(
            cursor,
            invoice["id"],
            contractor_id,
            accruals,
        )

    cursor.execute(
        """
        SELECT EXISTS (
            SELECT 1
            FROM contractor_invoice_obligations
            WHERE invoice_id = %s
        ) AS has_obligations
        """,
        (invoice["id"],),
    )

    if not cursor.fetchone()["has_obligations"]:
        return None

    return _prepare_invoice(
        cursor,
        invoice,
        contractor_email,
    )


def _process_contractor(
    contractor_id: int,
    issued_at: datetime,
) -> list[dict[str, Any]]:
    """Process all due billing periods for one contractor."""

    generated: list[dict[str, Any]] = []

    with get_connection() as connection:
        with connection.cursor() as cursor:
            # Serialize billing runs for this contractor.
            cursor.execute(
                """
                SELECT id, billing_day, email
                FROM contractors
                WHERE id = %s
                FOR UPDATE
                """,
                (contractor_id,),
            )

            contractor = cursor.fetchone()

            if contractor is None:
                return generated

            billing_day = contractor["billing_day"]

            if (
                not isinstance(billing_day, int)
                or not 1 <= billing_day <= 28
            ):
                raise ValueError(
                    f"Contractor {contractor_id} has an invalid billing day."
                )

            cursor.execute(
                """
                SELECT MIN(a.accrued_at) AS earliest_accrual
                FROM job_fee_accruals a
                WHERE a.contractor_id = %s
                  AND a.accrual_status = 'ACCRUED'
                  AND a.fee_amount > 0

                  AND NOT EXISTS (
                      SELECT 1
                      FROM contractor_invoice_obligation_accruals l
                      WHERE l.fee_accrual_id = a.id
                  )

                  AND NOT EXISTS (
                      SELECT 1
                      FROM contractor_invoice_items li
                      WHERE li.fee_accrual_id = a.id
                  )
                """,
                (contractor_id,),
            )

            earliest = cursor.fetchone()["earliest_accrual"]

            if earliest is None:
                return generated

            latest_cutoff = _latest_due_billing_date(
                issued_at.astimezone(BILLING_TIMEZONE).date(),
                billing_day,
            )

            cutoff = _first_cutoff_after(
                earliest,
                billing_day,
            )

            while cutoff <= latest_cutoff:
                period_start = _previous_billing_date(
                    cutoff,
                    billing_day,
                )

                invoice_result = _process_billing_period(
                    cursor,
                    contractor_id,
                    contractor["email"],
                    period_start,
                    cutoff,
                    issued_at,
                )

                if invoice_result is not None:
                    generated.append(invoice_result)

                cutoff = _next_billing_date(
                    cutoff,
                    billing_day,
                )

    return generated


def generate_due_contractor_invoices(
    as_of: datetime | None = None,
) -> dict[str, Any]:
    """
    Generate and issue all due contractor invoices.

    Each contractor is processed in an independent transaction.
    A failure for one contractor does not roll back another
    contractor's successfully issued invoice.

    The function can be called by a scheduled Lambda or manually
    by an authorized administrative workflow.
    """

    issued_at = as_of or datetime.now(BILLING_TIMEZONE)

    if issued_at.tzinfo is None:
        raise ValueError(
            "The invoice processing timestamp must be timezone-aware."
        )

    issued_at = issued_at.astimezone(BILLING_TIMEZONE)

    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT id
                FROM contractors
                WHERE billing_day BETWEEN 1 AND 28
                ORDER BY id
                """
            )

            contractor_ids = [
                row["id"]
                for row in cursor.fetchall()
            ]

    generated: list[dict[str, Any]] = []
    failed: list[dict[str, Any]] = []

    for contractor_id in contractor_ids:
        try:
            generated.extend(
                _process_contractor(
                    contractor_id,
                    issued_at,
                )
            )

        except Exception as error:
            failed.append({
                "contractor_id": contractor_id,
                "error_type": type(error).__name__,
                "error": str(error)[:1000],
            })

    return {
        "processed_at": issued_at,
        "contractor_count": len(contractor_ids),
        "generated_count": len(generated),
        "generated_invoices": generated,
        "failed_count": len(failed),
        "failed_contractors": failed,
    }
