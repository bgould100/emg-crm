
from app.database.connection import get_connection
from app.database.contractors import create_contractor
from app.database.leads import create_lead
from app.services.matching import route_opportunities_to_fill_slots
from app.services.opportunities import respond_to_opportunity
from app.services.job_status import update_job_status
from app.services.slot_release import release_accepted_bid_slot
from decimal import Decimal

def get_opportunity_contractor_pk(opportunity_id):
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT contractor_id
                FROM opportunities
                WHERE opportunity_id = %s
                """,
                (opportunity_id,),
            )
            row = cursor.fetchone()

    assert row is not None, f"Opportunity {opportunity_id} not found."
    return row["contractor_id"]

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
                get_opportunity_contractor_pk(opportunity["opportunity_id"]),
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
                            DELETE FROM status_history
                            WHERE contractor_id IN (
                                SELECT id FROM contractors
                                WHERE contractor_id = ANY(%s)
                            )
                            """,
                            (contractor_ids,),
                        )
                        cursor.execute(
                            """
                            DELETE FROM contractors
                            WHERE contractor_id = ANY(%s)
                            """,
                            (contractor_ids,),
                        )

        print("Test data cleaned up.")

def test_accepting_opportunity_creates_job_linked_to_that_opportunity():
    lead_id = None
    contractor_ids = []

    try:
        # Create a test lead requesting one bid.
        lead = create_lead(
            zip_code="83814",
            service_type="Roofing",
            description="Temporary test for job-opportunity linkage.",
            requested_bid_count=1,
            customer_name="Temporary Linkage Test",
            customer_email="linkage-test@example.com",
        )
        lead_id = lead["lead_id"]

        # Create one eligible contractor.
        contractor = create_contractor(
            company_name="EMG Opportunity Linkage Test",
            email="linkage-test@example.com",
            service_area=["83814"],
            specialties=["Roofing"],
        )
        contractor_ids.append(contractor["contractor_id"])

        # Route an opportunity to that contractor.
        opportunities = route_opportunities_to_fill_slots(lead_id)

        assert len(opportunities) == 1, (
            f"Expected one opportunity; got {len(opportunities)}"
        )

        opportunity_id = opportunities[0]["opportunity_id"]

        # Accept it through the real service.
        result = respond_to_opportunity(
            opportunity_id,
            "accept",
            get_opportunity_contractor_pk(opportunity_id),
        )

        assert result["status"] == "ACCEPTED"
        assert result["job_id"].startswith("EMGJ-")

        # Verify the job points to the exact opportunity that was accepted.
        with get_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT
                        j.job_id,
                        j.opportunity_id,
                        j.referral_fee_rate,
                        o.opportunity_id AS accepted_opportunity_id,
                        o.status AS opportunity_status
                    FROM jobs j
                    JOIN opportunities o ON o.id = j.opportunity_id
                    WHERE j.job_id = %s
                    """,
                    (result["job_id"],),
                )
                job = cursor.fetchone()

        assert job is not None, "The accepted opportunity's job was not found."
        assert job["opportunity_id"] is not None
        assert job["accepted_opportunity_id"] == opportunity_id
        assert job["opportunity_status"] == "ACCEPTED"
        assert job["referral_fee_rate"] == Decimal("0.1000")

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
                            DELETE FROM status_history
                            WHERE contractor_id IN (
                                SELECT id FROM contractors
                                WHERE contractor_id = ANY(%s)
                            )
                            """,
                            (contractor_ids,),
                        )
                        cursor.execute(
                            """
                            DELETE FROM contractors
                            WHERE contractor_id = ANY(%s)
                            """,
                            (contractor_ids,),
                        )


def test_admin_release_frees_capacity_and_preserves_accepted_job():
    lead_id = None
    contractor_ids = []

    try:
        lead = create_lead(
            zip_code="83814",
            service_type="Roofing",
            description="Test accepted slot release and replacement routing.",
            requested_bid_count=1,
            customer_name="Slot Release Integration Test",
            customer_email="slot-release-test@example.com",
        )
        lead_id = lead["lead_id"]

        for number in range(3):
            contractor = create_contractor(
                company_name=f"EMG Slot Release Test {number}",
                email=f"slot-release-test-{number}@example.com",
                service_area=["83814"],
                specialties=["Roofing"],
            )
            contractor_ids.append(contractor["contractor_id"])

        initial = route_opportunities_to_fill_slots(lead_id)
        assert len(initial) == 1
        original_opportunity_id = initial[0]["opportunity_id"]
        original_contractor_pk = get_opportunity_contractor_pk(
            original_opportunity_id
        )

        accepted = respond_to_opportunity(
            original_opportunity_id,
            "accept",
            original_contractor_pk,
        )
        assert accepted["status"] == "ACCEPTED"
        job_id = accepted["job_id"]

        # A contractor reporting BID_LOST does not free the accepted slot.
        job_update = update_job_status(
            job_id,
            "BID_LOST",
            original_contractor_pk,
        )
        assert job_update["status"] == "BID_LOST"

        no_replacements = route_opportunities_to_fill_slots(lead_id)
        assert no_replacements == []

        # Explicit administrative release frees the slot and routes a replacement.
        release_result = release_accepted_bid_slot(
            original_opportunity_id,
            reason="Contractor accepted in error",
            performed_by="slot-release-test-admin",
        )

        assert release_result["slot_released"] is True
        assert release_result["replacement_count"] == 1
        replacement = release_result["replacement_opportunities"][0]
        assert replacement["opportunity_id"] != original_opportunity_id

        replacement_contractor_pk = get_opportunity_contractor_pk(
            replacement["opportunity_id"]
        )
        assert replacement_contractor_pk != original_contractor_pk

        with get_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT
                        o.status AS opportunity_status,
                        o.slot_released,
                        o.slot_released_reason,
                        o.slot_released_by,
                        o.slot_released_at,
                        j.job_id,
                        j.status AS job_status
                    FROM opportunities o
                    JOIN jobs j ON j.opportunity_id = o.id
                    WHERE o.opportunity_id = %s
                    """,
                    (original_opportunity_id,),
                )
                original = cursor.fetchone()

                cursor.execute(
                    """
                    SELECT event_type, previous_status, new_status, metadata
                    FROM status_history
                    WHERE opportunity_id = (
                        SELECT id
                        FROM opportunities
                        WHERE opportunity_id = %s
                    )
                      AND event_type = 'AcceptedBidSlotReleased'
                    ORDER BY id DESC
                    LIMIT 1
                    """,
                    (original_opportunity_id,),
                )
                audit = cursor.fetchone()

                cursor.execute(
                    """
                    SELECT COUNT(*) AS pending_count
                    FROM opportunities
                    WHERE lead_id = (
                        SELECT id FROM leads WHERE lead_id = %s
                    )
                      AND status = 'PENDING'
                    """,
                    (lead_id,),
                )
                pending_count = cursor.fetchone()["pending_count"]

        assert original is not None
        assert original["opportunity_status"] == "ACCEPTED"
        assert original["slot_released"] is True
        assert original["slot_released_reason"] == "Contractor accepted in error"
        assert original["slot_released_by"] == "slot-release-test-admin"
        assert original["slot_released_at"] is not None
        assert original["job_id"] == job_id
        assert original["job_status"] == "BID_LOST"

        assert audit is not None
        assert audit["event_type"] == "AcceptedBidSlotReleased"
        assert audit["previous_status"] == "ACCEPTED"
        assert audit["new_status"] == "ACCEPTED"
        assert audit["metadata"]["reason"] == "Contractor accepted in error"
        assert audit["metadata"]["performed_by"] == "slot-release-test-admin"
        assert pending_count == 1

        # A second release must be rejected and must not route another invitation.
        try:
            release_accepted_bid_slot(
                original_opportunity_id,
                reason="Attempt to release again",
                performed_by="slot-release-test-admin",
            )
        except ValueError as error:
            assert "already been released" in str(error)
        else:
            raise AssertionError("Repeated release should have been rejected.")

        with get_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT COUNT(*) AS pending_count
                    FROM opportunities
                    WHERE lead_id = (
                        SELECT id FROM leads WHERE lead_id = %s
                    )
                      AND status = 'PENDING'
                    """,
                    (lead_id,),
                )
                assert cursor.fetchone()["pending_count"] == 1

    finally:
        # Delete only this test's lead, related records, and contractors.
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
                            DELETE FROM status_history
                            WHERE contractor_id IN (
                                SELECT id FROM contractors
                                WHERE contractor_id = ANY(%s)
                            )
                            """,
                            (contractor_ids,),
                        )
                        cursor.execute(
                            """
                            DELETE FROM contractors
                            WHERE contractor_id = ANY(%s)
                            """,
                            (contractor_ids,),
                        )
