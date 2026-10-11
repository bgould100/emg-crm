import json
from typing import Any

from app.database.connection import get_connection
from app.services.matching import route_opportunities_to_fill_slots


def release_accepted_bid_slot(
    opportunity_id: str,
    reason: str,
    performed_by: str,
) -> dict[str, Any]:
    """Release bid capacity while preserving the accepted opportunity and job."""

    reason = reason.strip()
    performed_by = performed_by.strip()

    if len(reason) < 3:
        raise ValueError("A reason of at least 3 characters is required.")

    if not performed_by:
        raise ValueError("Administrator identity is required.")

    with get_connection() as connection:
        with connection.cursor() as cursor:
            # Find the lead so we can acquire locks in the project's
            # established lead-first order.
            cursor.execute(
                """
                SELECT
                    o.id AS opportunity_pk,
                    o.lead_id AS lead_pk
                FROM opportunities o
                WHERE o.opportunity_id = %s
                """,
                (opportunity_id,),
            )
            initial = cursor.fetchone()

            if initial is None:
                raise LookupError(
                    f"Opportunity {opportunity_id} was not found."
                )

            cursor.execute(
                """
                SELECT id, lead_id
                FROM leads
                WHERE id = %s
                FOR UPDATE
                """,
                (initial["lead_pk"],),
            )
            lead = cursor.fetchone()

            if lead is None:
                raise LookupError(
                    f"Opportunity {opportunity_id} was not found."
                )

            # Recheck and lock the opportunity after locking its lead.
            cursor.execute(
                """
                SELECT
                    id,
                    opportunity_id,
                    lead_id,
                    contractor_id,
                    status,
                    slot_released
                FROM opportunities
                WHERE id = %s
                  AND lead_id = %s
                FOR UPDATE
                """,
                (initial["opportunity_pk"], lead["id"]),
            )
            opportunity = cursor.fetchone()

            if opportunity is None:
                raise LookupError(
                    f"Opportunity {opportunity_id} was not found."
                )

            if opportunity["status"] != "ACCEPTED":
                raise ValueError(
                    "Only an accepted opportunity can have its slot released."
                )

            if opportunity["slot_released"]:
                raise ValueError(
                    "The accepted bid slot has already been released."
                )

            cursor.execute(
                """
                UPDATE opportunities
                SET slot_released = TRUE,
                    slot_released_at = CURRENT_TIMESTAMP,
                    slot_released_reason = %s,
                    slot_released_by = %s
                WHERE id = %s
                """,
                (reason, performed_by, opportunity["id"]),
            )

            cursor.execute(
                """
                INSERT INTO status_history (
                    lead_id,
                    contractor_id,
                    opportunity_id,
                    event_type,
                    previous_status,
                    new_status,
                    metadata
                )
                VALUES (
                    %s, %s, %s,
                    'AcceptedBidSlotReleased',
                    'ACCEPTED',
                    'ACCEPTED',
                    %s::jsonb
                )
                """,
                (
                    opportunity["lead_id"],
                    opportunity["contractor_id"],
                    opportunity["id"],
                    json.dumps({
                        "reason": reason,
                        "performed_by": performed_by,
                    }),
                ),
            )

            lead_id = lead["lead_id"]

    # The release and audit record commit before routing is attempted.
    # If routing fails, the released slot remains recorded and routing
    # can be retried separately.
    replacements = route_opportunities_to_fill_slots(lead_id)

    return {
        "opportunity_id": opportunity_id,
        "action": "SLOT_RELEASED",
        "slot_released": True,
        "replacement_count": len(replacements),
        "replacement_opportunities": replacements,
    }



def retry_replacement_routing(
    opportunity_id: str,
    reason: str,
    performed_by: str,
) -> dict[str, Any]:
    """Retry replacement routing for an already-released accepted slot."""

    reason = reason.strip()
    performed_by = performed_by.strip()

    if len(reason) < 3:
        raise ValueError("A reason of at least 3 characters is required.")
    if not performed_by:
        raise ValueError("Administrator identity is required.")

    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT id AS opportunity_pk, lead_id AS lead_pk
                FROM opportunities
                WHERE opportunity_id = %s
                """,
                (opportunity_id,),
            )
            initial = cursor.fetchone()

            if initial is None:
                raise LookupError(
                    f"Opportunity {opportunity_id} was not found."
                )

            cursor.execute(
                """
                SELECT id, lead_id
                FROM leads
                WHERE id = %s
                FOR UPDATE
                """,
                (initial["lead_pk"],),
            )
            lead = cursor.fetchone()

            if lead is None:
                raise LookupError(
                    f"Opportunity {opportunity_id} was not found."
                )

            cursor.execute(
                """
                SELECT id, lead_id, contractor_id, status, slot_released
                FROM opportunities
                WHERE id = %s AND lead_id = %s
                FOR UPDATE
                """,
                (initial["opportunity_pk"], lead["id"]),
            )
            opportunity = cursor.fetchone()

            if opportunity is None:
                raise LookupError(
                    f"Opportunity {opportunity_id} was not found."
                )
            if opportunity["status"] != "ACCEPTED":
                raise ValueError(
                    "Only an accepted opportunity can have routing retried."
                )
            if not opportunity["slot_released"]:
                raise ValueError(
                    "The accepted bid slot must be released before retrying routing."
                )

            cursor.execute(
                """
                INSERT INTO status_history (
                    lead_id, contractor_id, opportunity_id,
                    event_type, previous_status, new_status, metadata
                )
                VALUES (
                    %s, %s, %s,
                    'AcceptedBidSlotRoutingRetried',
                    'ACCEPTED', 'ACCEPTED', %s::jsonb
                )
                """,
                (
                    opportunity["lead_id"],
                    opportunity["contractor_id"],
                    opportunity["id"],
                    json.dumps({
                        "reason": reason,
                        "performed_by": performed_by,
                    }),
                ),
            )
            lead_id = lead["lead_id"]

    # The retry audit commits before routing. If routing fails again,
    # the released slot remains released and another retry is possible.
    replacements = route_opportunities_to_fill_slots(lead_id)

    return {
        "opportunity_id": opportunity_id,
        "action": "RETRY_SLOT_ROUTING",
        "replacement_count": len(replacements),
        "replacement_opportunities": replacements,
    }
