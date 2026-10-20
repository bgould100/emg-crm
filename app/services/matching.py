
import secrets
from typing import Any

from app.database.connection import get_connection


MINIMUM_MATCH_SCORE = 70


def route_opportunities_to_fill_slots(
    lead_id: str,
) -> list[dict[str, Any]]:
    """
    Create enough contractor opportunities to fill a lead's open bid slots.

    Open slots = requested bids - accepted opportunities - pending
    opportunities.

    Each contractor can receive only one opportunity per lead.
    New opportunities expire seven days after creation.
    """

    created_opportunities: list[dict[str, Any]] = []

    with get_connection() as connection:
        with connection.cursor() as cursor:
            # Lock the lead while calculating and filling its open slots.
            cursor.execute(
                """
                SELECT
                    id,
                    lead_id,
                    zip_code,
                    service_type,
                    requested_bid_count
                FROM leads
                WHERE lead_id = %s
                FOR UPDATE
                """,
                (lead_id,),
            )
            lead = cursor.fetchone()

            if lead is None:
                raise LookupError(f"Lead {lead_id} was not found.")

            # Count accepted and pending invitations.
            cursor.execute(
                """
                SELECT
                    COUNT(*) FILTER (
                        WHERE status = 'ACCEPTED'
                          AND slot_released = FALSE
                    ) AS accepted,
                    COUNT(*) FILTER (
                        WHERE status = 'PENDING'
                    ) AS pending
                FROM opportunities
                WHERE lead_id = %s
                """,
                (lead["id"],),
            )
            counts = cursor.fetchone()

            open_slots = (
                lead["requested_bid_count"]
                - counts["accepted"]
                - counts["pending"]
            )

            if open_slots <= 0:
                return []

            # Find active, available contractors who have not already
            # received an opportunity for this lead.
            cursor.execute(
                """
                SELECT
                    c.id,
                    c.contractor_id,
                    c.company_name,
                    c.service_area,
                    c.specialties
                FROM contractors c
                WHERE c.status = 'ACTIVE'
                  AND c.availability = 'AVAILABLE'
                  AND NOT EXISTS (
                      SELECT 1
                      FROM opportunities o
                      WHERE o.lead_id = %s
                        AND o.contractor_id = c.id
                  )
                """,
                (lead["id"],),
            )
            contractors = cursor.fetchall()

            matches = []

            for contractor in contractors:
                specialties = {
                    item.strip().casefold()
                    for item in contractor["specialties"].split(",")
                }

                service_areas = {
                    item.strip()
                    for item in contractor["service_area"].split(",")
                }

                # Matching score:
                # Active and available: 20 points
                # Matching specialty: 40 points
                # Matching ZIP code: 30 points
                
                specialty_match = (
                    lead["service_type"].strip().casefold() in specialties
                )
                zip_match = lead["zip_code"].strip() in service_areas

                # Preserve the existing scoring system.
                score = 20
                if specialty_match:
                    score += 40
                if zip_match:
                    score += 30

                # Both conditions are mandatory, regardless of the score.
                if specialty_match and zip_match and score >= MINIMUM_MATCH_SCORE:
                    matches.append((score, contractor))


            # Highest score first; company name breaks ties consistently.
            matches.sort(
                key=lambda item: (
                    -item[0],
                    item[1]["company_name"].casefold(),
                )
            )

            # Create no more invitations than there are open slots.
            for score, contractor in matches[:open_slots]:
                opportunity_id = (
                    f"EMGO-{secrets.token_hex(5).upper()}"
                )

                cursor.execute(
                    """
                    INSERT INTO opportunities (
                        opportunity_id,
                        lead_id,
                        contractor_id,
                        status,
                        match_score,
                        sent_at,
                        expires_at
                    )
                    VALUES (
                        %s, %s, %s, 'PENDING', %s,
                        CURRENT_TIMESTAMP,
                        CURRENT_TIMESTAMP + INTERVAL '7 days'
                    )
                    ON CONFLICT (lead_id, contractor_id) DO NOTHING
                    RETURNING id, opportunity_id, status, match_score
                    """,
                    (
                        opportunity_id,
                        lead["id"],
                        contractor["id"],
                        score,
                    ),
                )

                # IMPORTANT: retrieve the inserted row before using it.
                opportunity = cursor.fetchone()

                # A conflict means this contractor already has an
                # opportunity for this lead, so skip it safely.
                if opportunity is None:
                    continue

                # Record the new opportunity in the audit history.
                cursor.execute(
                    """
                    INSERT INTO status_history (
                        lead_id,
                        contractor_id,
                        opportunity_id,
                        event_type,
                        new_status,
                        metadata
                    )
                    VALUES (
                        %s, %s, %s,
                        'OpportunityCreated',
                        'PENDING',
                        jsonb_build_object('match_score', %s)
                    )
                    """,
                    (
                        lead["id"],
                        contractor["id"],
                        opportunity["id"],
                        score,
                    ),
                )

                created_opportunities.append(
                    {
                        "opportunity_id": opportunity["opportunity_id"],
                        "lead_id": lead["lead_id"],
                        "contractor_id": contractor["contractor_id"],
                        "company_name": contractor["company_name"],
                        "match_score": opportunity["match_score"],
                        "status": opportunity["status"],
                    }
                )

    return created_opportunities


def route_next_opportunity(
    lead_id: str,
) -> dict[str, Any] | None:
    """
    Compatibility wrapper for callers expecting a single result.

    The routing engine fills all currently open slots, while this wrapper
    returns only the first newly created opportunity.
    """

    opportunities = route_opportunities_to_fill_slots(lead_id)

    return opportunities[0] if opportunities else None
