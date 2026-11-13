
import json
from decimal import Decimal
from typing import Any, Literal

from app.database.connection import get_connection
from app.services.payment_agreements import (
    _installments_from_saved_agreement,
)
from app.services.review_tokens import (
    generate_review_token,
    hash_review_token,
)


JobStatus = Literal[
    "QUOTE_IN_PROGRESS",
    "BID_WON",
    "BID_LOST",
    "JOB_IN_PROGRESS",
    "JOB_COMPLETED",
    "PAYMENT_RECEIVED",
]


ALLOWED_TRANSITIONS = {
    "QUOTE_IN_PROGRESS": {"BID_WON", "BID_LOST"},
    "BID_WON": {"JOB_IN_PROGRESS"},
    "BID_LOST": set(),
    "JOB_IN_PROGRESS": {"JOB_COMPLETED"},
    "JOB_COMPLETED": {"PAYMENT_RECEIVED"},
    "PAYMENT_RECEIVED": set(),
}


def update_job_status(
    job_id: str,
    new_status: JobStatus,
    contractor_pk: int,
) -> dict[str, Any]:
    """Update one contractor's job and record any required EMG alert."""

    if new_status not in ALLOWED_TRANSITIONS:
        raise ValueError("Unsupported job status.")

    with get_connection() as connection:
        with connection.cursor() as cursor:
            # Find the homeowner lead only through this contractor's job.
            cursor.execute(
                """
                SELECT lead_id
                FROM jobs
                WHERE job_id = %s
                  AND contractor_id = %s
                """,
                (job_id, contractor_pk),
            )
            initial_record = cursor.fetchone()

            if initial_record is None:
                raise LookupError("Job not found.")

            lead_pk = initial_record["lead_id"]

            # Preserve the lead-first lock order used by the opportunity
            # service so updates to the same homeowner project serialize.
            cursor.execute(
                """
                SELECT id
                FROM leads
                WHERE id = %s
                FOR UPDATE
                """,
                (lead_pk,),
            )

            if cursor.fetchone() is None:
                raise LookupError("Job not found.")

            # Recheck ownership and lock the specific job.
            cursor.execute(
                """
                SELECT
                    j.id AS job_pk,
                    j.job_id,
                    j.status AS current_status,
                    j.lead_id AS lead_pk,
                    j.contractor_id AS contractor_pk,
                    l.lead_id AS public_lead_id,
                    l.customer_email,
                    c.contractor_id AS public_contractor_id,
                    c.company_name
                FROM jobs j
                JOIN leads l ON l.id = j.lead_id
                JOIN contractors c ON c.id = j.contractor_id
                WHERE j.job_id = %s
                  AND j.lead_id = %s
                  AND j.contractor_id = %s
                FOR UPDATE OF j
                """,
                (job_id, lead_pk, contractor_pk),
            )
            record = cursor.fetchone()

            if record is None:
                raise LookupError("Job not found.")

            current_status = record["current_status"]

            # Make retries of an already completed status request harmless.
            if current_status == new_status:
                return {
                    "job_id": record["job_id"],
                    "status": current_status,
                    "changed": False,
                    "winning_contractors": None,
                    "alert_queued": False,
                }

            if new_status not in ALLOWED_TRANSITIONS.get(
                current_status, set()
            ):
                raise ValueError(
                    f"Cannot change job status from {current_status} "
                    f"to {new_status}."
                )
            if new_status == "JOB_COMPLETED":
                # The latest agreement version must be operative.
                cursor.execute(
                    """
                    SELECT
                        id,
                        version,
                        arrangement_type,
                        invoice_total,
                        down_payment_amount,
                        down_payment_due_date,
                        plan_start_date,
                        schedule_type,
                        installment_count,
                        installment_amount,
                        final_installment_amount,
                        final_due_date,
                        custom_schedule,
                        status,
                        admin_revised_at
                    FROM job_payment_agreements
                    WHERE job_id = %s
                    ORDER BY version DESC, id DESC
                    LIMIT 1
                    """,
                    (record["job_pk"],),
                )
                agreement = cursor.fetchone()

                if agreement is None:
                    raise ValueError(
                        "A payment agreement must be submitted and "
                        "approved before the job can be completed."
                    )

                if agreement["status"] not in (
                    "ACCEPTED",
                    "ADMIN_REVISED",
                ):
                    raise ValueError(
                        "The latest payment agreement is not approved. "
                        "Accept the agreement or wait for EMG to finalize "
                        "the revised terms before completing the job."
                    )

                if agreement["arrangement_type"] == "PAID_IN_FULL":
                    # Only active, contractor-reported collections count.
                    cursor.execute(
                        """
                        SELECT COALESCE(SUM(amount), 0) AS collected_total
                        FROM job_collections
                        WHERE job_id = %s
                          AND report_status = 'ACTIVE'
                        """,
                        (record["job_pk"],),
                    )
                    collection_record = cursor.fetchone()

                    if (
                        collection_record["collected_total"]
                        < agreement["invoice_total"]
                    ):
                        raise ValueError(
                            "The reported collections do not yet cover "
                            "the full invoice total. Record the remaining "
                            "payment before completing the job."
                        )

                elif agreement["arrangement_type"] == "PAYMENT_PLAN":
                    # Reconstruct the authoritative schedule from the
                    # approved agreement terms.
                    collected_total = Decimal("0.00")

                    if agreement["status"] == "ADMIN_REVISED":
                        cursor.execute(
                            """
                            SELECT COALESCE(SUM(amount), 0) AS collected_total
                            FROM job_collections
                            WHERE job_id = %s
                              AND report_status = 'ACTIVE'
                              AND reported_at <= %s
                            """,
                            (
                                record["job_pk"],
                                agreement["admin_revised_at"],
                            ),
                        )
                        collection_record = cursor.fetchone()
                        collected_total = Decimal(
                            str(collection_record["collected_total"])
                        )
                    try:
                        expected_installments = (
                            _installments_from_saved_agreement(
                                agreement,
                                collected_total=collected_total,
                            )
                        )
                    except (KeyError, TypeError, ValueError) as exc:
                        raise ValueError(
                            "The approved payment agreement contains "
                            "invalid schedule terms. Please contact EMG "
                            "before completing the job."
                        ) from exc

                    # Retrieve every non-cancelled installment record.
                    cursor.execute(
                        """
                        SELECT
                            installment_number,
                            installment_type,
                            principal_amount,
                            due_date
                        FROM job_installments
                        WHERE agreement_id = %s
                          AND status <> 'CANCELLED'
                        ORDER BY installment_number
                        """,
                        (agreement["id"],),
                    )
                    saved_installments = cursor.fetchall()

                    # The saved rows must match the calculated schedule
                    # exactly, including every amount and due date.
                    schedule_matches = (
                        len(saved_installments)
                        == len(expected_installments)
                    )

                    if schedule_matches:
                        for saved, expected in zip(
                            saved_installments,
                            expected_installments,
                        ):
                            if (
                                saved["installment_number"]
                                != expected["installment_number"]
                                or saved["installment_type"]
                                != expected["installment_type"]
                                or saved["principal_amount"]
                                != expected["principal_amount"]
                                or saved["due_date"]
                                != expected["due_date"]
                            ):
                                schedule_matches = False
                                break

                    if not schedule_matches:
                        raise ValueError(
                            "The approved payment plan does not match "
                            "its complete calculated installment schedule. "
                            "Please contact EMG before completing the job."
                        )

                else:
                    raise ValueError(
                        "The latest payment agreement has an unsupported "
                        "arrangement type. Please contact EMG."
                    )

            cursor.execute(
                """
                UPDATE jobs
                SET status = %s,
                    photos_locked_at = CASE
                        WHEN %s = 'JOB_COMPLETED'
                            THEN COALESCE(
                                photos_locked_at,
                                CURRENT_TIMESTAMP
                            )
                        ELSE photos_locked_at
                    END,
                    notes_locked_at = CASE
                        WHEN %s = 'JOB_COMPLETED'
                            THEN COALESCE(
                                notes_locked_at,
                                CURRENT_TIMESTAMP
                            )
                        ELSE notes_locked_at
                    END,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = %s
                """,
                (new_status, new_status, new_status, record["job_pk"]),
            )

            cursor.execute(
                """
                INSERT INTO status_history (
                    lead_id,
                    contractor_id,
                    job_id,
                    event_type,
                    previous_status,
                    new_status
                )
                VALUES (
                    %s, %s, %s,
                    'JobStatusChanged',
                    %s, %s
                )
                """,
                (
                    record["lead_pk"],
                    record["contractor_pk"],
                    record["job_pk"],
                    current_status,
                    new_status,
                ),
            )

            if new_status == "JOB_COMPLETED":
                # Snapshot the homeowner's email when the job is completed.
                customer_email = (
                    record["customer_email"] or ""
                ).strip() or None

                delivery_status = (
                    "PENDING" if customer_email else "SKIPPED_NO_EMAIL"
                )
                retry_state = (
                    "ACTIVE" if customer_email else "COMPLETE"
                )

                cursor.execute(
                    """
                    INSERT INTO homeowner_review_invitations (
                        job_id,
                        customer_email,
                        delivery_status,
                        expires_at,
                        retry_state,
                        next_attempt_at
                    )
                    VALUES (
                        %s,
                        %s,
                        %s,
                        CURRENT_TIMESTAMP + INTERVAL '30 days',
                        %s,
                        CASE
                            WHEN %s = 'ACTIVE'
                                THEN CURRENT_TIMESTAMP
                            ELSE NULL
                        END
                    )
                    RETURNING id
                    """,
                    (
                        record["job_pk"],
                        customer_email,
                        delivery_status,
                        retry_state,
                        retry_state,
                    ),
                )
                invitation = cursor.fetchone()
                token = generate_review_token(invitation["id"])
                token_hash = hash_review_token(token)

                cursor.execute(
                    """
                    UPDATE homeowner_review_invitations
                    SET token_hash = %s
                    WHERE id = %s
                    """,
                    (token_hash, invitation["id"]),
                )

            winning_contractors = None
            alert_queued = False

            if new_status == "BID_WON":
                cursor.execute(
                    """
                    SELECT COUNT(DISTINCT contractor_id) AS win_count
                    FROM jobs
                    WHERE lead_id = %s
                      AND status = 'BID_WON'
                    """,
                    (record["lead_pk"],),
                )
                winning_contractors = cursor.fetchone()["win_count"]

                # Each additional contractor who reports a win gets one
                # uniquely keyed event. Repeated requests from the same
                # contractor cannot create duplicate event records.
                if winning_contractors >= 2:
                    event_key = (
                        f"multiple-bid-wins:{record['public_lead_id']}:"
                        f"{record['public_contractor_id']}"
                    )

                    cursor.execute(
                        """
                        INSERT INTO outbox_events (
                            event_key,
                            event_type,
                            payload
                        )
                        VALUES (%s, %s, %s::jsonb)
                        ON CONFLICT (event_key) DO NOTHING
                        """,
                        (
                            event_key,
                            "MultipleContractorsReportedBidWon",
                            json.dumps(
                                {
                                    "lead_id": record["public_lead_id"],
                                    "job_id": record["job_id"],
                                    "contractor_id": (
                                        record["public_contractor_id"]
                                    ),
                                    "company_name": record["company_name"],
                                    "winning_contractor_count": (
                                        winning_contractors
                                    ),
                                }
                            ),
                        ),
                    )
                    alert_queued = cursor.rowcount == 1

    return {
        "job_id": job_id,
        "status": new_status,
        "changed": True,
        "winning_contractors": winning_contractors,
        "alert_queued": alert_queued,
    }
