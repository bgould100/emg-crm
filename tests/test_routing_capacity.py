
from app.database.connection import get_connection
from app.database.contractors import create_contractor
from app.database.leads import create_lead
from app.services.matching import route_opportunities_to_fill_slots
from app.services.opportunities import respond_to_opportunity


def test_five_bid_slots_are_replenished_after_declines():
    lead_id = None
    contractor_ids = []

    try:
        # Create a test homeowner lead requesting five bids.
        lead = create_lead(
            zip_code="83814",
            service_type="Roofing",
            description="Temporary test lead for routing capacity.",
            requested_bid_count=5,
            customer_name="Temporary Routing Test",
            customer_email="routing-test@example.com",
        )
        lead_id = lead["lead_id"]

        # Create seven eligible contractors.
        for number in range(7):
            contractor = create_contractor(
                company_name=f"EMG Routing Test {number}",
                email=f"routing-test-{number}@example.com",
                service_area=["83814"],
                specialties=["Roofing"],
            )
            contractor_ids.append(contractor["contractor_id"])

        # Initially fill all five requested bid slots.
        initial = route_opportunities_to_fill_slots(lead_id)

        assert len(initial) == 5, (
            f"Expected 5 initial opportunities; got {len(initial)}"
        )
        print("PASS: Five initial opportunities created.")

        # Each decline should immediately create one replacement.
        for opportunity in initial[:2]:
            result = respond_to_opportunity(
                opportunity["opportunity_id"],
                "decline",
            )
            replacements = result["next_opportunities"]

            assert len(replacements) == 1, (
                "Expected one replacement after each decline; "
                f"got {len(replacements)}"
            )

            print(
                "PASS: Decline immediately created replacement "
                f"{replacements[0]['opportunity_id']}."
            )

        # Verify the final opportunity counts for this lead.
        with get_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT
                        COUNT(*) FILTER (
                            WHERE o.status = 'PENDING'
                        ) AS pending,
                        COUNT(*) FILTER (
                            WHERE o.status = 'DECLINED'
                        ) AS declined,
                        COUNT(*) FILTER (
                            WHERE o.status = 'ACCEPTED'
                        ) AS accepted
                    FROM opportunities o
                    JOIN leads l ON l.id = o.lead_id
                    WHERE l.lead_id = %s
                    """,
                    (lead_id,),
                )
                counts = cursor.fetchone()

        assert counts["pending"] == 5, (
            f"Expected 5 pending opportunities; got {counts['pending']}"
        )
        assert counts["declined"] == 2, (
            f"Expected 2 declined opportunities; got {counts['declined']}"
        )
        assert counts["accepted"] == 0, (
            f"Expected 0 accepted opportunities; got {counts['accepted']}"
        )

        print(
            "PASS: Final counts are "
            f"{counts['pending']} pending, "
            f"{counts['declined']} declined, "
            f"{counts['accepted']} accepted."
        )

    finally:
        # Remove only records created for this test.
        if lead_id:
            with get_connection() as connection:
                with connection.cursor() as cursor:
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

        print("Test data cleaned up.")