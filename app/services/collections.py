from datetime import datetime, timezone
from decimal import Decimal, ROUND_HALF_UP

from psycopg.errors import UniqueViolation
from psycopg.types.json import Jsonb

from app.database.connection import get_connection


FEE_RATE = Decimal("0.10")
FEE_CAP = Decimal("5000.00")
CENT = Decimal("0.01")


class JobNotFoundError(Exception):
    pass


class IdempotencyConflictError(Exception):
    pass


def _money(value: Decimal) -> Decimal:
    return value.quantize(CENT, rounding=ROUND_HALF_UP)


def _result(
    collection,
    accrual,
    public_job_id: str,
    replay: bool = False,
) -> dict:
    return {
        "collection_id": collection["id"],
        "fee_accrual_id": accrual["id"],
        "job_id": public_job_id,
        "amount": str(collection["amount"]),
        "collected_at": collection["collected_at"].isoformat(),
        "fee_amount": str(accrual["fee_amount"]),
        "cumulative_fee_after": str(accrual["cumulative_fee_after"]),
        "idempotent_replay": replay,
    }


def record_collection(
    *,
    public_job_id: str,
    contractor_pk: int,
    account_id: int,
    amount: Decimal,
    collected_at: datetime,
    idempotency_key: str,
) -> dict:
    """Record a contractor-reported collection and its fee accrual atomically."""

    if not isinstance(amount, Decimal) or not amount.is_finite() or amount <= 0:
        raise ValueError("Payment amount must be a positive decimal amount.")

    if amount != _money(amount):
        raise ValueError("Payment amount must have no more than two decimal places.")

    amount = _money(amount)

    if collected_at.tzinfo is None or collected_at.utcoffset() is None:
        raise ValueError("collected_at must include a timezone.")

    collected_at = collected_at.astimezone(timezone.utc)

    if collected_at > datetime.now(timezone.utc):
        raise ValueError("A payment cannot be reported before it is collected.")

    if not idempotency_key or len(idempotency_key) > 100:
        raise ValueError("Provide an idempotency key of 1 to 100 characters.")

    try:
        with get_connection() as connection:
            with connection.cursor() as cursor:
                # Find the job only within the authenticated contractor's account.
                cursor.execute(
                    """
                    SELECT id, lead_id, job_id
                    FROM jobs
                    WHERE job_id = %s AND contractor_id = %s
                    """,
                    (public_job_id, contractor_pk),
                )
                initial_job = cursor.fetchone()

                if initial_job is None:
                    raise JobNotFoundError()

                # Preserve the existing lead-before-job lock order.
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
                    raise JobNotFoundError()

                # A retry returns the original records without creating another accrual.
                cursor.execute(
                    """
                    SELECT
                        c.id AS collection_id,
                        c.job_id,
                        c.amount,
                        c.collected_at,
                        c.idempotency_key,
                        a.id AS accrual_id,
                        a.fee_amount,
                        a.cumulative_fee_after
                    FROM job_collections c
                    JOIN job_fee_accruals a ON a.collection_id = c.id
                    WHERE c.contractor_id = %s
                      AND c.idempotency_key = %s
                    """,
                    (contractor_pk, idempotency_key),
                )
                previous = cursor.fetchone()

                if previous is not None:
                    same_request = (
                        previous["job_id"] == job["id"]
                        and previous["amount"] == amount
                        and previous["collected_at"] == collected_at
                    )
                    if not same_request:
                        raise IdempotencyConflictError(
                            "This idempotency key was already used for a different payment."
                        )

                    return {
                        "collection_id": previous["collection_id"],
                        "fee_accrual_id": previous["accrual_id"],
                        "job_id": job["job_id"],
                        "amount": str(previous["amount"]),
                        "collected_at": previous["collected_at"].isoformat(),
                        "fee_amount": str(previous["fee_amount"]),
                        "cumulative_fee_after": str(
                            previous["cumulative_fee_after"]
                        ),
                        "idempotent_replay": True,
                    }

                cursor.execute(
                    """
                    SELECT COALESCE(SUM(a.fee_amount), 0) AS total_fee
                    FROM job_fee_accruals a
                    JOIN job_collections c ON c.id = a.collection_id
                    WHERE a.job_id = %s
                      AND c.report_status = 'ACTIVE'
                    """,
                    (job["id"],),
                )
                cumulative_before = _money(cursor.fetchone()["total_fee"])

                remaining_cap = max(
                    Decimal("0.00"),
                    FEE_CAP - cumulative_before,
                )
                eligible_amount = amount
                fee_amount = min(
                    _money(eligible_amount * FEE_RATE),
                    remaining_cap,
                )
                cumulative_after = _money(cumulative_before + fee_amount)

                cursor.execute(
                    """
                    INSERT INTO job_collections (
                        job_id, contractor_id, reported_by_account_id,
                        amount, collected_at, idempotency_key
                    )
                    VALUES (%s, %s, %s, %s, %s, %s)
                    RETURNING id, job_id, amount, collected_at
                    """,
                    (
                        job["id"],
                        contractor_pk,
                        account_id,
                        amount,
                        collected_at,
                        idempotency_key,
                    ),
                )
                collection = cursor.fetchone()

                cursor.execute(
                    """
                    INSERT INTO job_fee_accruals (
                        job_id, contractor_id, collection_id,
                        collection_amount, eligible_collection_amount,
                        fee_rate, fee_cap_snapshot,
                        cumulative_fee_before, cumulative_fee_after,
                        fee_amount
                    )
                    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    RETURNING id, fee_amount, cumulative_fee_after
                    """,
                    (
                        job["id"],
                        contractor_pk,
                        collection["id"],
                        amount,
                        eligible_amount,
                        FEE_RATE,
                        FEE_CAP,
                        cumulative_before,
                        cumulative_after,
                        fee_amount,
                    ),
                )
                accrual = cursor.fetchone()

                # Mark the latest agreement paid in full only when
                # active, recorded collections reach its invoice total.
                cursor.execute(
                    """
                    SELECT COALESCE(SUM(amount), 0) AS collected_total
                    FROM job_collections
                    WHERE job_id = %s
                      AND contractor_id = %s
                      AND report_status = 'ACTIVE'
                    """,
                    (job["id"], contractor_pk),
                )
                collected_total = _money(
                    cursor.fetchone()["collected_total"]
                )

                cursor.execute(
                    """
                    SELECT id, invoice_total
                    FROM job_payment_agreements
                    WHERE job_id = %s
                      AND contractor_id = %s
                    ORDER BY version DESC
                    LIMIT 1
                    FOR UPDATE
                    """,
                    (job["id"], contractor_pk),
                )
                current_agreement = cursor.fetchone()

                if (
                    current_agreement is not None
                    and collected_total >= current_agreement["invoice_total"]
                ):
                    cursor.execute(
                        """
                        UPDATE job_payment_agreements
                        SET paid_in_full_at = CURRENT_TIMESTAMP
                        WHERE id = %s
                          AND paid_in_full_at IS NULL
                        """,
                        (current_agreement["id"],),
                    )

                cursor.execute(
                    """
                    INSERT INTO job_financial_audit_events (
                        job_id, contractor_id, actor_type,
                        actor_account_id, event_type,
                        related_record_type, related_record_id, details
                    )
                    VALUES (
                        %s, %s, 'CONTRACTOR', %s,
                        'PAYMENT_COLLECTION_REPORTED',
                        'job_collections', %s, %s
                    )
                    """,
                    (
                        job["id"],
                        contractor_pk,
                        account_id,
                        collection["id"],
                        Jsonb({
                            "amount": str(amount),
                            "collected_at": collected_at.isoformat(),
                            "fee_amount": str(fee_amount),
                            "idempotency_key": idempotency_key,
                        }),
                    ),
                )

        return _result(collection, accrual, job["job_id"])

    except UniqueViolation as error:
        # Only translate the idempotency index violation into a conflict.
        # Other unique-constraint failures must remain visible as real errors.
        if (
            error.diag.constraint_name
            == "uq_job_collections_contractor_idempotency"
        ):
            raise IdempotencyConflictError(
                "This idempotency key has already been used."
            ) from error
        raise

