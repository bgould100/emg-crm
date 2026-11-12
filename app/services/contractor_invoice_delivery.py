
"""Load and prepare contractor invoice delivery data."""
import json
import uuid
from typing import Any
from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo
from botocore.exceptions import ClientError

from app.database.connection import get_connection
from app.services.contractor_invoice_email import (
    InvoiceEmailPreparationError,
    send_contractor_invoice_email,
)
from app.services.contractor_invoice_pdf import (
    render_contractor_invoice_pdf,
)

def _is_definitive_ses_rejection(error: Exception) -> bool:
    """
    Return True only when SES explicitly rejects the request
    with a client-side 4xx response.
    """

    if not isinstance(error, ClientError):
        return False

    status_code = error.response.get(
        "ResponseMetadata", {}
    ).get("HTTPStatusCode")

    return (
        isinstance(status_code, int)
        and 400 <= status_code < 500
    )

def load_claimed_invoice_delivery(
    delivery_id: int,
    claim_token: str,
) -> dict[str, Any]:
    """
    Load the invoice, contractor, recipient, and job details for a
    delivery currently claimed by this worker.

    The claim must still be active when the data is retrieved.
    """

    if delivery_id <= 0:
        raise ValueError("A valid delivery ID is required.")

    if not claim_token:
        raise ValueError("A delivery claim token is required.")

    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT
                    d.id AS delivery_id,
                    d.recipient_email,
                    d.delivery_status,
                    d.claim_token,

                    i.id AS invoice_id,
                    i.invoice_number,
                    i.billing_period_start,
                    i.billing_period_end,
                    i.total_amount,
                    i.status AS invoice_status,

                    c.id AS contractor_pk,
                    c.contractor_id,
                    c.company_name,
                    c.contact_name

                FROM contractor_invoice_deliveries d

                JOIN contractor_invoices i
                    ON i.id = d.invoice_id

                JOIN contractors c
                    ON c.id = i.contractor_id

                WHERE d.id = %s
                  AND d.delivery_status = 'IN_PROGRESS'
                  AND d.claim_token = %s::uuid
                  AND d.claimed_until > CURRENT_TIMESTAMP
                  AND i.status = 'DRAFT'

                """,
                (
                    delivery_id,
                    claim_token,
                ),
            )

            delivery = cursor.fetchone()

            if delivery is None:
                raise RuntimeError(
                    "The invoice delivery is not actively claimed "
                    "by this worker, or the invoice is no longer "
                    "eligible for delivery."
                )

            if not delivery["recipient_email"].strip():
                raise RuntimeError(
                    "The claimed invoice delivery has no recipient email."
                )

            cursor.execute(
                """
                SELECT
                    j.job_id,
                    l.service_type,
                    l.zip_code,
                    o.original_amount

                FROM contractor_invoice_obligations o

                JOIN jobs j
                    ON j.id = o.job_id
                   AND j.contractor_id = o.contractor_id

                JOIN leads l
                    ON l.id = j.lead_id

                WHERE o.invoice_id = %s
                  AND o.contractor_id = %s

                ORDER BY j.job_id
                """,
                (
                    delivery["invoice_id"],
                    delivery["contractor_pk"],
                ),
            )

            obligations = cursor.fetchall()

            if not obligations:
                raise RuntimeError(
                    "The invoice has no job-level obligations."
                )

            invoice = {
                "id": delivery["invoice_id"],
                "invoice_number": delivery["invoice_number"],
                "billing_period_start": (
                    delivery["billing_period_start"]
                ),
                "billing_period_end": (
                    delivery["billing_period_end"]
                ),
                "total_amount": delivery["total_amount"],
            }

            contractor = {
                "contractor_id": delivery["contractor_id"],
                "company_name": delivery["company_name"],
                "contact_name": delivery["contact_name"],
            }

            return {
                "delivery_id": delivery["delivery_id"],
                "recipient_email": delivery["recipient_email"],
                "invoice": invoice,
                "contractor": contractor,
                "obligations": obligations,
            }

LEASE_MINUTES = 5
MAX_BATCH_SIZE = 25


def _claim_due_invoice_deliveries(
    batch_size: int = 25,
) -> list[dict[str, Any]]:
    """
    Claim eligible invoice deliveries in a short transaction.

    Expired IN_PROGRESS claims are deliberately excluded. Their
    delivery outcome must be resolved as UNKNOWN, not automatically
    retried.
    """

    if not 1 <= batch_size <= MAX_BATCH_SIZE:
        raise ValueError(
            f"Batch size must be between 1 and {MAX_BATCH_SIZE}."
        )

    claimed_deliveries: list[dict[str, Any]] = []

    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT id
                FROM contractor_invoice_deliveries
                WHERE delivery_status IN ('PENDING', 'FAILED')
                  AND COALESCE(next_attempt_at, created_at)
                      <= CURRENT_TIMESTAMP
                ORDER BY
                    COALESCE(next_attempt_at, created_at),
                    created_at,
                    id
                LIMIT %s
                FOR UPDATE SKIP LOCKED
                """,
                (batch_size,),
            )

            selected = cursor.fetchall()

            for row in selected:
                claim_token = uuid.uuid4()

                cursor.execute(
                    """
                    UPDATE contractor_invoice_deliveries
                    SET delivery_status = 'IN_PROGRESS',
                        attempt_count = attempt_count + 1,
                        last_attempt_at = CURRENT_TIMESTAMP,
                        claimed_until =
                            CURRENT_TIMESTAMP
                            + (%s * INTERVAL '1 minute'),
                        claim_token = %s,
                        next_attempt_at = NULL,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = %s
                      AND delivery_status IN ('PENDING', 'FAILED')
                    RETURNING
                        id,
                        recipient_email,
                        attempt_count,
                        failure_streak,
                        claimed_until,
                        claim_token
                    """,
                    (
                        LEASE_MINUTES,
                        claim_token,
                        row["id"],
                    ),
                )

                delivery = cursor.fetchone()

                if delivery is None:
                    continue

                cursor.execute(
                    """
                    INSERT INTO contractor_invoice_delivery_events (
                        delivery_id,
                        event_type,
                        attempt_number,
                        details
                    )
                    VALUES (
                        %s,
                        'ATTEMPT_STARTED',
                        %s,
                        %s::jsonb
                    )
                    """,
                    (
                        delivery["id"],
                        delivery["attempt_count"],
                        json.dumps({
                            "lease_minutes": LEASE_MINUTES,
                        }),
                    ),
                )

                claimed_deliveries.append({
                    "delivery_id": delivery["id"],
                    "recipient_email": delivery["recipient_email"],
                    "attempt_count": delivery["attempt_count"],
                    "failure_streak": delivery["failure_streak"],
                    "claimed_until": delivery["claimed_until"],
                    "claim_token": str(delivery["claim_token"]),
                })

    return claimed_deliveries


def _mark_expired_invoice_deliveries_unknown(
    batch_size: int = 25,
) -> list[int]:
    """
    Mark expired delivery claims as UNKNOWN.

    An expired claim does not prove that SES failed to send the email.
    These deliveries must not be automatically retried.
    """

    if not 1 <= batch_size <= MAX_BATCH_SIZE:
        raise ValueError(
            f"Batch size must be between 1 and {MAX_BATCH_SIZE}."
        )

    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                WITH expired AS (
                    SELECT
                        id,
                        attempt_count
                    FROM contractor_invoice_deliveries
                    WHERE delivery_status = 'IN_PROGRESS'
                      AND claimed_until <= CURRENT_TIMESTAMP
                    ORDER BY claimed_until, id
                    LIMIT %s
                    FOR UPDATE SKIP LOCKED
                ),
                updated AS (
                    UPDATE contractor_invoice_deliveries AS delivery
                    SET delivery_status = 'UNKNOWN',
                        claimed_until = NULL,
                        claim_token = NULL,
                        last_error =
                            'Delivery claim expired before its outcome '
                            'could be confirmed.',
                        updated_at = CURRENT_TIMESTAMP
                    FROM expired
                    WHERE delivery.id = expired.id
                    RETURNING
                        delivery.id,
                        expired.attempt_count
                )
                INSERT INTO contractor_invoice_delivery_events (
                    delivery_id,
                    event_type,
                    attempt_number,
                    details
                )
                SELECT
                    id,
                    'OUTCOME_UNKNOWN',
                    attempt_count,
                    jsonb_build_object(
                        'reason',
                        'Delivery lease expired before confirmation'
                    )
                FROM updated
                RETURNING delivery_id
                """,
                (batch_size,),
            )

            return [
                row["delivery_id"]
                for row in cursor.fetchall()
            ]


BILLING_TIMEZONE = ZoneInfo("America/Los_Angeles")


def _record_invoice_delivery_success(
    delivery: dict[str, Any],
    message_id: str,
    sent_at: datetime,
    due_at: datetime,
) -> bool:
    """
    Record SES acceptance and issue the invoice atomically.

    The invoice becomes SENT only if this worker still owns
    an active delivery claim.
    """

    if not message_id or not message_id.strip():
        raise ValueError("A valid SES message ID is required.")

    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE contractor_invoice_deliveries
                SET delivery_status = 'SENT',
                    provider_message_id = %s,
                    failure_streak = 0,
                    claimed_until = NULL,
                    claim_token = NULL,
                    last_error = NULL,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = %s
                  AND delivery_status = 'IN_PROGRESS'
                  AND claim_token = %s::uuid
                  AND claimed_until > CURRENT_TIMESTAMP
                RETURNING invoice_id, attempt_count
                """,
                (
                    message_id,
                    delivery["delivery_id"],
                    delivery["claim_token"],
                ),
            )

            updated_delivery = cursor.fetchone()

            if updated_delivery is None:
                return False

            cursor.execute(
                """
                UPDATE contractor_invoices
                SET status = 'SENT',
                    sent_at = %s,
                    due_at = %s
                WHERE id = %s
                  AND status = 'DRAFT'
                  AND sent_at IS NULL
                  AND due_at IS NULL
                """,
                (
                    sent_at,
                    due_at,
                    updated_delivery["invoice_id"],
                ),
            )

            if cursor.rowcount != 1:
                raise RuntimeError(
                    "Invoice could not be issued after SES acceptance. "
                    "The transaction will be rolled back."
                )

            cursor.execute(
                """
                INSERT INTO contractor_invoice_delivery_events (
                    delivery_id,
                    event_type,
                    attempt_number,
                    details
                )
                VALUES (
                    %s,
                    'ATTEMPT_SUCCEEDED',
                    %s,
                    %s::jsonb
                )
                """,
                (
                    delivery["delivery_id"],
                    updated_delivery["attempt_count"],
                    json.dumps({
                        "provider_message_id": message_id,
                        "sent_at": sent_at.isoformat(),
                        "due_at": due_at.isoformat(),
                    }),
                ),
            )

    return True

def _record_invoice_delivery_failure(
    delivery: dict[str, Any],
    error: Exception,
) -> bool:
    """
    Pause a delivery after a known processing or provider failure.

    The invoice remains DRAFT. No automatic retry is scheduled.
    """

    error_message = (
        str(error).strip()
        or "Invoice delivery processing failed."
    )
    error_message = error_message[:2000]

    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE contractor_invoice_deliveries
                SET delivery_status = 'PAUSED',
                    failure_streak = failure_streak + 1,
                    paused_at = CURRENT_TIMESTAMP,
                    claimed_until = NULL,
                    claim_token = NULL,
                    next_attempt_at = NULL,
                    last_error = %s,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = %s
                  AND delivery_status = 'IN_PROGRESS'
                  AND claim_token = %s::uuid
                RETURNING attempt_count, failure_streak
                """,
                (
                    error_message,
                    delivery["delivery_id"],
                    delivery["claim_token"],
                ),
            )

            updated_delivery = cursor.fetchone()

            if updated_delivery is None:
                return False

            event_details = json.dumps({
                "error": error_message,
                "failure_streak": updated_delivery["failure_streak"],
            })

            cursor.execute(
                """
                INSERT INTO contractor_invoice_delivery_events (
                    delivery_id,
                    event_type,
                    attempt_number,
                    details
                )
                VALUES
                    (%s, 'ATTEMPT_FAILED', %s, %s::jsonb),
                    (%s, 'PAUSED', %s, %s::jsonb)
                """,
                (
                    delivery["delivery_id"],
                    updated_delivery["attempt_count"],
                    event_details,
                    delivery["delivery_id"],
                    updated_delivery["attempt_count"],
                    event_details,
                ),
            )

    return True

def _record_invoice_delivery_unknown(
    delivery: dict[str, Any],
    error: Exception,
) -> bool:
    """
    Mark a delivery UNKNOWN when SES may have accepted the email,
    but the outcome cannot be confirmed.

    No automatic retry is scheduled.
    """

    error_message = (
        str(error).strip()
        or "The email delivery outcome could not be confirmed."
    )
    error_message = error_message[:2000]

    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE contractor_invoice_deliveries
                SET delivery_status = 'UNKNOWN',
                    claimed_until = NULL,
                    claim_token = NULL,
                    next_attempt_at = NULL,
                    last_error = %s,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = %s
                  AND delivery_status = 'IN_PROGRESS'
                  AND claim_token = %s::uuid
                RETURNING attempt_count
                """,
                (
                    error_message,
                    delivery["delivery_id"],
                    delivery["claim_token"],
                ),
            )

            updated_delivery = cursor.fetchone()

            if updated_delivery is None:
                return False

            cursor.execute(
                """
                INSERT INTO contractor_invoice_delivery_events (
                    delivery_id,
                    event_type,
                    attempt_number,
                    details
                )
                VALUES (
                    %s,
                    'OUTCOME_UNKNOWN',
                    %s,
                    %s::jsonb
                )
                """,
                (
                    delivery["delivery_id"],
                    updated_delivery["attempt_count"],
                    json.dumps({
                        "error": error_message,
                    }),
                ),
            )

    return True

def _deliver_claimed_invoice_delivery(
    claim: dict[str, Any],
) -> str:
    """
    Process one claimed invoice delivery.

    Returns the resulting delivery outcome.
    """

    try:
        loaded = load_claimed_invoice_delivery(
            claim["delivery_id"],
            claim["claim_token"],
        )

        delivery = {**loaded, **claim}

        sent_at = datetime.now(BILLING_TIMEZONE)
        due_date = sent_at.date() + timedelta(days=7)
        due_at = datetime.combine(
            due_date + timedelta(days=1),
            time.min,
            tzinfo=BILLING_TIMEZONE,
        )

        invoice = delivery["invoice"]

        pdf_bytes = render_contractor_invoice_pdf(
            invoice=invoice,
            contractor=delivery["contractor"],
            obligations=delivery["obligations"],
            issue_date=sent_at.date(),
            due_date=due_at.date(),
        )

        billing_period_start = invoice[
            "billing_period_start"
        ].isoformat()

        billing_period_end = invoice[
            "billing_period_end"
        ].isoformat()

        total_amount = format(
            invoice["total_amount"],
            ",.2f",
        )

    except Exception as exc:
        return (
            "PAUSED"
            if _record_invoice_delivery_failure(claim, exc)
            else "CLAIM_LOST"
        )

    try:
        message_id = send_contractor_invoice_email(
            recipient_email=delivery["recipient_email"],
            invoice_number=invoice["invoice_number"],
            billing_period_start=billing_period_start,
            billing_period_end=billing_period_end,
            total_amount=total_amount,
            pdf_bytes=pdf_bytes,
        )

    except InvoiceEmailPreparationError as exc:
        paused = _record_invoice_delivery_failure(claim, exc)
        return "PAUSED" if paused else "CLAIM_LOST"

    except Exception as exc:
        if _is_definitive_ses_rejection(exc):
            return (
                "PAUSED"
                if _record_invoice_delivery_failure(claim, exc)
                else "CLAIM_LOST"
            )

        unknown = _record_invoice_delivery_unknown(claim, exc)
        return "UNKNOWN" if unknown else "CLAIM_LOST"

    try:
        recorded = _record_invoice_delivery_success(
            delivery,
            message_id,
            sent_at,
            due_at,
        )

    except Exception as exc:
        unknown_error = RuntimeError(
            "SES accepted the email, but the database could not "
            f"confirm invoice issuance: {exc}"
        )

        return (
            "UNKNOWN"
            if _record_invoice_delivery_unknown(
                claim,
                unknown_error,
            )
            else "CLAIM_LOST"
        )

    if not recorded:
        unknown_error = RuntimeError(
            "SES accepted the email, but the delivery claim "
            "was no longer valid when success was recorded."
        )

        return (
            "UNKNOWN"
            if _record_invoice_delivery_unknown(
                claim,
                unknown_error,
            )
            else "CLAIM_LOST"
        )

    return "SENT"

def deliver_contractor_invoice_batch(
    batch_size: int = 25,
) -> dict[str, Any]:
    """
    Process one batch of due contractor invoice deliveries.

    Expired claims are marked UNKNOWN before new deliveries
    are claimed. No delivery is automatically retried when
    its previous outcome is uncertain.
    """

    expired_ids = _mark_expired_invoice_deliveries_unknown(
        batch_size=batch_size,
    )

    claims = _claim_due_invoice_deliveries(
        batch_size=batch_size,
    )

    results = {
        "expired_marked_unknown": expired_ids,
        "claimed": len(claims),
        "sent": 0,
        "paused": 0,
        "unknown": 0,
        "claim_lost": 0,
        "processing_errors": 0,
    }

    for claim in claims:
        try:
            outcome = _deliver_claimed_invoice_delivery(claim)
        except Exception:
            results["processing_errors"] += 1
            continue

        if outcome == "SENT":
            results["sent"] += 1
        elif outcome == "PAUSED":
            results["paused"] += 1
        elif outcome == "UNKNOWN":
            results["unknown"] += 1
        elif outcome == "CLAIM_LOST":
            results["claim_lost"] += 1
        else:
            results["processing_errors"] += 1

    return results