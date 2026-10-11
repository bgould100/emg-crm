
from app.database.connection import get_connection
from app.database.contractors import create_contractor
from app.database.leads import create_lead
from app.services.matching import route_opportunities_to_fill_slots
from app.services.opportunities import respond_to_opportunity


def test_service_rejects_opportunity_owned_by_another_contractor():
    lead_id = None
    contractor_ids = []

    try:
        lead = create_lead(
            zip_code="83814",
            service_type="Roofing",
            description="Temporary service authorization test.",
            requested_bid_count=1,
            customer_name="Temporary Service Auth Test",
            customer_email="service-auth-test@example.com",
        )
        lead_id = lead["lead_id"]

        contractor_a = create_contractor(
            company_name="EMG Service Auth Contractor A",
            email="service-auth-a@example.com",
            service_area=["83814"],
            specialties=["Roofing"],
        )
        contractor_ids.append(contractor_a["contractor_id"])

        contractor_b = create_contractor(
            company_name="EMG Service Auth Contractor B",
            email="service-auth-b@example.com",
            service_area=["83814"],
            specialties=["Roofing"],
        )
        contractor_ids.append(contractor_b["contractor_id"])

        opportunities = route_opportunities_to_fill_slots(lead_id)

        opportunity_a = next(
            (
                opportunity
                for opportunity in opportunities
                if opportunity["contractor_id"]
                == contractor_a["contractor_id"]
            ),
            None,
        )

        assert opportunity_a is not None, (
            "Routing did not create an opportunity for Contractor A."
        )

        # Retrieve Contractor B's internal database ID.
        with get_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT id
                    FROM contractors
                    WHERE contractor_id = %s
                    """,
                    (contractor_b["contractor_id"],),
                )
                contractor_b_row = cursor.fetchone()

        assert contractor_b_row is not None
        contractor_b_pk = contractor_b_row["id"]

        # B attempts to act directly on A's opportunity.
        try:
            respond_to_opportunity(
                opportunity_a["opportunity_id"],
                "decline",
                contractor_b_pk,
            )
        except LookupError:
            pass
        else:
            raise AssertionError(
                "The service allowed Contractor B to respond to "
                "Contractor A's opportunity."
            )

        # Confirm the opportunity was not changed.
        with get_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT status
                    FROM opportunities
                    WHERE opportunity_id = %s
                    """,
                    (opportunity_a["opportunity_id"],),
                )
                opportunity_row = cursor.fetchone()

        assert opportunity_row is not None
        assert opportunity_row["status"] == "PENDING"

    finally:
        # Clean up only the temporary records created by this test.
        with get_connection() as connection:
            with connection.cursor() as cursor:
                if lead_id:
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
