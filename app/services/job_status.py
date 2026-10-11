
import json
from typing import Any, Literal

from app.database.connection import get_connection


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

            cursor.execute(
                """
                UPDATE jobs
                SET status = %s,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = %s
                """,
                (new_status, record["job_pk"]),
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
