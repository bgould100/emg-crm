
from app.database.connection import get_connection
from app.database.contractors import create_contractor
from app.database.leads import create_lead
from app.services.matching import route_opportunities_to_fill_slots
from app.services.expiration import expire_overdue_opportunities


def test_expired_opportunity_creates_refill_event():
    lead_id = None
    contractor_ids = []
    original_id = None

    try:
        lead = create_lead(
            zip_code="99501",
            service_type="Test Window Repair",
            description="Temporary test for expiration processing.",
            requested_bid_count=1,
        )
        lead_id = lead["lead_id"]

        for number in range(2):
            contractor = create_contractor(
                company_name=f"EMG Expiration Test {number}",
                email=(
                    f"expiration-test-{number}-"
                    f"{lead_id.lower()}@example.com"
                ),
                service_area=["99501"],
                specialties=["Test Window Repair"],
            )
            contractor_ids.append(contractor["contractor_id"])

        initial = route_opportunities_to_fill_slots(lead_id)

        assert len(initial) == 1
        original_id = initial[0]["opportunity_id"]

        with get_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE opportunities
                    SET expires_at =
                        CURRENT_TIMESTAMP - INTERVAL '1 minute'
                    WHERE opportunity_id = %s
                      AND status = 'PENDING'
                    """,
                    (original_id,),
                )
                assert cursor.rowcount == 1

        result = expire_overdue_opportunities()

        assert result["expired_count"] == 1
        assert original_id in result["expired_opportunity_ids"]
        assert result["outbox_event_count"] == 1

        with get_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT status
                    FROM opportunities
                    WHERE opportunity_id = %s
                    """,
                    (original_id,),
                )
                opportunity = cursor.fetchone()

                cursor.execute(
                    """
                    SELECT event_key, event_type, payload, processed_at
                    FROM outbox_events
                    WHERE event_key = %s
                    """,
                    (f"opportunity-expired:{original_id}:refill",),
                )
                outbox_event = cursor.fetchone()

                cursor.execute(
                    """
                    SELECT COUNT(*) AS event_count
                    FROM status_history sh
                    JOIN leads l ON l.id = sh.lead_id
                    JOIN opportunities o ON o.id = sh.opportunity_id
                    WHERE l.lead_id = %s
                      AND o.opportunity_id = %s
                      AND sh.event_type = 'OpportunityExpired'
                    """,
                    (lead_id, original_id),
                )
                history = cursor.fetchone()

        assert opportunity is not None
        assert opportunity["status"] == "EXPIRED"

        assert outbox_event is not None
        assert outbox_event["event_type"] == "OpportunityExpired"
        assert outbox_event["payload"]["lead_id"] == lead_id
        assert (
            outbox_event["payload"]["expired_opportunity_id"]
            == original_id
        )
        assert outbox_event["processed_at"] is None

        assert history["event_count"] == 1

    finally:
        if lead_id:
            with get_connection() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        DELETE FROM outbox_events
                        WHERE payload->>'lead_id' = %s
                        """,
                        (lead_id,),
                    )

                    cursor.execute(
                        "SELECT id FROM leads WHERE lead_id = %s",
                        (lead_id,),
                    )
                    lead_row = cursor.fetchone()

                    if lead_row:
                        db_lead_id = lead_row["id"]

                        cursor.execute(
                            "DELETE FROM status_history WHERE lead_id = %s",
                            (db_lead_id,),
                        )
                        cursor.execute(
                            "DELETE FROM photos WHERE lead_id = %s",
                            (db_lead_id,),
                        )
                        cursor.execute(
                            "DELETE FROM jobs WHERE lead_id = %s",
                            (db_lead_id,),
                        )
                        cursor.execute(
                            "DELETE FROM opportunities WHERE lead_id = %s",
                            (db_lead_id,),
                        )
                        cursor.execute(
                            "DELETE FROM leads WHERE id = %s",
                            (db_lead_id,),
                        )

                    if contractor_ids:
                        cursor.execute(
                            """
                            DELETE FROM contractors
                            WHERE contractor_id = ANY(%s)
                            """,
                            (contractor_ids,),
                        )