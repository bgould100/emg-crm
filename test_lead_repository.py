
from app.database.connection import get_connection
from app.database.leads import create_lead, get_lead_by_lead_id


def main():
    lead = create_lead(
        zip_code="83814",
        service_type="Roofing",
        description="TEST ONLY - Verify EMG database insert and retrieval.",
        requested_bid_count=2,
        budget=7500.00,
        urgency="NORMAL",
        customer_name="EMG Test Customer",
        customer_email="test@example.com",
        customer_phone="555-0100",
    )

    print("Lead created:")
    print(f"  ID: {lead['lead_id']}")
    print(f"  ZIP: {lead['zip_code']}")
    print(f"  Service: {lead['service_type']}")
    print(f"  Requested bids: {lead['requested_bid_count']}")

    retrieved = get_lead_by_lead_id(lead["lead_id"])

    assert retrieved is not None, "Lead could not be retrieved."
    assert retrieved["service_type"] == "Roofing"
    assert retrieved["requested_bid_count"] == 2

    print("\nLead retrieved successfully.")
    print("Insert and retrieval checks passed.")

    # Remove the test record so it cannot enter the real lead pipeline.
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "DELETE FROM leads WHERE lead_id = %s",
                (lead["lead_id"],),
            )

    print("Test record cleaned up.")


if __name__ == "__main__":
    main()