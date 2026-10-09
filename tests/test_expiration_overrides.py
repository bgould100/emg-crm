
from app.database.connection import get_connection
from app.database.contractors import create_contractor
from app.database.leads import create_lead
from app.services.matching import route_opportunities_to_fill_slots
from app.services.expiration import (
    expire_overdue_opportunities,
    hold_opportunity,
    extend_opportunity_deadline,
    release_opportunity_hold,
)


def test_hold_extend_release_and_expire():
    lead_id = None
    contractor_ids = []
    original_id = None

    try:
        # Create a temporary lead requesting one bid.
        lead = create_lead(
            zip_code="99502",
            service_type="Test Gutter Repair",
            description="Temporary test for expiration overrides.",
            requested_bid_count=1,
        )
        lead_id = lead["lead_id"]

        # Create eligible contractors so replacements can be routed later.
        for number in range(3):
            contractor = create_contractor(
                company_name=f"EMG Override Test {number}",
                email=(
                    f"override-test-{number}-"
                    f"{lead_id.lower()}@example.com"
                ),
                service_area=["99502"],
                specialties=["Test Gutter Repair"],
            )
            contractor_ids.append(contractor["contractor_id"])

        # Route the first opportunity.
        initial = route_opportunities_to_fill_slots(lead_id)

        assert len(initial) == 1
        original_id = initial[0]["opportunity_id"]

        # 1. Place the opportunity on hold.
        held = hold_opportunity(
            original_id,
            reason="Testing administrative hold",
            performed_by="test-admin",
        )

        assert held["expiration_paused"] is True

        # Force its deadline into the past.
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

        # The expiration service must skip an opportunity on hold.
        held_result = expire_overdue_opportunities()

        assert held_result["expired_count"] == 0
        assert held_result["outbox_event_count"] == 0

        with get_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT status, expiration_paused
                    FROM opportunities
                    WHERE opportunity_id = %s
                    """,
                    (original_id,),
                )
                held_record = cursor.fetchone()

        assert held_record["status"] == "PENDING"
        assert held_record["expiration_paused"] is True

        print("PASS: A held opportunity was not expired.")

        # 2. Extend the deadline by two days.
        extended = extend_opportunity_deadline(
            original_id,
            days=2,
            reason="Contractor requested more time",
            performed_by="test-admin",
        )

        assert extended["extension_days"] == 2

        with get_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT expires_at > CURRENT_TIMESTAMP AS deadline_future
                    FROM opportunities
                    WHERE opportunity_id = %s
                    """,
                    (original_id,),
                )
                deadline = cursor.fetchone()

        assert deadline["deadline_future"] is True

        print("PASS: The deadline extension moved expiration into the future.")

        # 3. Release the hold.
        released = release_opportunity_hold(
            original_id,
            reason="Review complete",
            performed_by="test-admin",
        )

        assert released["expiration_paused"] is False

        # The extended deadline means the opportunity should remain pending.
        after_release = expire_overdue_opportunities()

        assert after_release["expired_count"] == 0
        assert after_release["outbox_event_count"] == 0

        print(
            "PASS: Releasing the hold resumed monitoring "
            "without early expiration."
        )

        # 4. Make the deadline overdue and run expiration again.
        with get_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE opportunities
                    SET expires_at =
                        CURRENT_TIMESTAMP - INTERVAL '1 minute'
                    WHERE opportunity_id = %s
                      AND status = 'PENDING'
                      AND expiration_paused = FALSE
                    """,
                    (original_id,),
                )
                assert cursor.rowcount == 1

        final_result = expire_overdue_opportunities()

        assert final_result["expired_count"] == 1
        assert original_id in final_result["expired_opportunity_ids"]
        assert final_result["outbox_event_count"] == 1

        # Verify the opportunity was expired and recorded in history.
        with get_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT status, expiration_paused
                    FROM opportunities
                    WHERE opportunity_id = %s
                    """,
                    (original_id,),
                )
                expired_record = cursor.fetchone()

                cursor.execute(
                    """
                    SELECT COUNT(*) AS event_count
                    FROM status_history sh
                    JOIN leads l ON l.id = sh.lead_id
                    WHERE l.lead_id = %s
                      AND sh.event_type = 'OpportunityExpired'
                      AND sh.opportunity_id = (
                          SELECT id
                          FROM opportunities
                          WHERE opportunity_id = %s
                      )
                    """,
                    (lead_id, original_id),
                )
                history = cursor.fetchone()

                # Verify the durable outbox event was committed.
                cursor.execute(
                    """
                    SELECT event_key, event_type, payload, processed_at
                    FROM outbox_events
                    WHERE event_key = %s
                    """,
                    (f"opportunity-expired:{original_id}:refill",),
                )
                outbox_event = cursor.fetchone()

        assert expired_record is not None
        assert expired_record["status"] == "EXPIRED"
        assert expired_record["expiration_paused"] is False

        assert history["event_count"] == 1

        assert outbox_event is not None
        assert outbox_event["event_type"] == "OpportunityExpired"
        assert outbox_event["payload"]["lead_id"] == lead_id
        assert (
            outbox_event["payload"]["expired_opportunity_id"]
            == original_id
        )
        assert outbox_event["processed_at"] is None

        print("PASS: The overdue opportunity was marked EXPIRED.")
        print("PASS: Expiration was recorded in status history.")
        print("PASS: A durable, unprocessed outbox event was created.")
        print("PASS: Replacement routing is left to downstream processing.")

    finally:
        # Remove only records created for this test.
        if lead_id:
            with get_connection() as connection:
                with connection.cursor() as cursor:
                    # Delete outbox events before deleting their lead.
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
                            """
                            DELETE FROM status_history
                            WHERE lead_id = %s
                            """,
                            (db_lead_id,),
                        )
                        cursor.execute(
                            """
                            DELETE FROM photos
                            WHERE lead_id = %s
                            """,
                            (db_lead_id,),
                        )
                        cursor.execute(
                            """
                            DELETE FROM jobs
                            WHERE lead_id = %s
                            """,
                            (db_lead_id,),
                        )
                        cursor.execute(
                            """
                            DELETE FROM opportunities
                            WHERE lead_id = %s
                            """,
                            (db_lead_id,),
                        )
                        cursor.execute(
                            """
                            DELETE FROM leads
                            WHERE id = %s
                            """,
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

        print("Expiration override test data cleaned up.")