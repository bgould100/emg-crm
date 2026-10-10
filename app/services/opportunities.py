
import secrets
from typing import Any, Literal

from app.database.connection import get_connection
from app.services.matching import route_opportunities_to_fill_slots


Decision = Literal["accept", "decline"]


def respond_to_opportunity(
    opportunity_id: str,
    decision: Decision,
) -> dict[str, Any]:
    """Record a contractor's decision and refill open bid slots."""

    if decision not in ("accept", "decline"):
        raise ValueError("Decision must be 'accept' or 'decline'.")

    with get_connection() as connection:
        with connection.cursor() as cursor:
            # First find the lead associated with this opportunity.
            # This lookup is intentionally unlocked: we acquire locks in
            # a consistent order below (lead first, opportunity second).
            cursor.execute(
                """
                SELECT lead_id
                FROM opportunities
                WHERE opportunity_id = %s
                """,
                (opportunity_id,),
            )
            initial_record = cursor.fetchone()

            if initial_record is None:
                raise LookupError("Opportunity not found.")

            lead_pk = initial_record["lead_id"]

            # Lock the lead first. The matching service also locks the lead,
            # helping serialize decisions that affect bid-slot capacity.
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
                raise LookupError("Lead not found.")

            # Re-read and lock the opportunity after locking its lead.
            cursor.execute(
                """
                SELECT
                    o.id AS opportunity_pk,
                    o.opportunity_id,
                    o.status AS opportunity_status,
                    o.lead_id AS lead_pk,
                    o.contractor_id AS contractor_pk,
                    l.lead_id AS lead_id,
                    l.customer_name,
                    l.customer_email,
                    l.customer_phone
                FROM opportunities o
                JOIN leads l ON l.id = o.lead_id
                WHERE o.opportunity_id = %s
                  AND o.lead_id = %s
                FOR UPDATE OF o
                """,
                (opportunity_id, lead_pk),
            )
            record = cursor.fetchone()

            if record is None:
                raise LookupError("Opportunity not found.")

            if record["opportunity_status"] != "PENDING":
                raise ValueError(
                    "This opportunity has already been responded to."
                )

            if decision == "accept":
                # Accept the opportunity within the same transaction
                # used to create its job and history entry.
                cursor.execute(
                    """
                    UPDATE opportunities
                    SET status = 'ACCEPTED',
                        responded_at = CURRENT_TIMESTAMP,
                        accepted_at = CURRENT_TIMESTAMP
                    WHERE id = %s
                    """,
                    (record["opportunity_pk"],),
                )

                job_id = f"EMGJ-{secrets.token_hex(5).upper()}"

                # Directly link the job to the accepted opportunity.
                # The database migration must be applied before this
                # insert can run against the updated schema.
                cursor.execute(
                    """
                    INSERT INTO jobs (
                        job_id,
                        opportunity_id,
                        lead_id,
                        contractor_id,
                        status
                    )
                    VALUES (%s, %s, %s, %s, 'QUOTE_IN_PROGRESS')
                    RETURNING id, job_id
                    """,
                    (
                        job_id,
                        record["opportunity_pk"],
                        record["lead_pk"],
                        record["contractor_pk"],
                    ),
                )
                job = cursor.fetchone()

                cursor.execute(
                    """
                    INSERT INTO status_history (
                        lead_id,
                        contractor_id,
                        opportunity_id,
                        job_id,
                        event_type,
                        previous_status,
                        new_status
                    )
                    VALUES (
                        %s, %s, %s, %s,
                        'OpportunityAccepted',
                        'PENDING',
                        'ACCEPTED'
                    )
                    """,
                    (
                        record["lead_pk"],
                        record["contractor_pk"],
                        record["opportunity_pk"],
                        job["id"],
                    ),
                )

                result = {
                    "opportunity_id": record["opportunity_id"],
                    "decision": decision,
                    "status": "ACCEPTED",
                    "job_id": job["job_id"],
                    "customer_name": record["customer_name"],
                    "customer_email": record["customer_email"],
                    "customer_phone": record["customer_phone"],
                }

            else:
                cursor.execute(
                    """
                    UPDATE opportunities
                    SET status = 'DECLINED',
                        responded_at = CURRENT_TIMESTAMP,
                        declined_at = CURRENT_TIMESTAMP
                    WHERE id = %s
                    """,
                    (record["opportunity_pk"],),
                )

                cursor.execute(
                    """
                    INSERT INTO status_history (
                        lead_id,
                        contractor_id,
                        opportunity_id,
                        event_type,
                        previous_status,
                        new_status
                    )
                    VALUES (
                        %s, %s, %s,
                        'OpportunityDeclined',
                        'PENDING',
                        'DECLINED'
                    )
                    """,
                    (
                        record["lead_pk"],
                        record["contractor_pk"],
                        record["opportunity_pk"],
                    ),
                )

                result = {
                    "opportunity_id": record["opportunity_id"],
                    "decision": decision,
                    "status": "DECLINED",
                }

            lead_id = record["lead_id"]

    # The decision is committed before routing replacement invitations.
    # The matcher recalculates accepted and pending counts and fills
    # any remaining slots it can.
    next_opportunities = route_opportunities_to_fill_slots(lead_id)

    result["next_opportunities"] = next_opportunities

    if next_opportunities:
        count = len(next_opportunities)
        result["message"] = (
            f"Created {count} additional contractor "
            f"opportunit{'y' if count == 1 else 'ies'}."
        )
    else:
        result["message"] = (
            "No additional opportunities were created. The requested "
            "bid count may already be covered, invitations may still be "
            "pending, or no eligible contractors may remain."
        )

    return result
