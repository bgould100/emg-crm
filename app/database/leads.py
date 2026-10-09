
import secrets
from typing import Any

from app.database.connection import get_connection


def create_lead(
    *,
    zip_code: str,
    service_type: str,
    description: str,
    requested_bid_count: int = 1,
    budget: float | None = None,
    urgency: str | None = None,
    customer_name: str | None = None,
    customer_email: str | None = None,
    customer_phone: str | None = None,
) -> dict[str, Any]:
    """Create a homeowner lead and return the saved database record."""

    if not zip_code.strip():
        raise ValueError("ZIP code is required.")

    if not service_type.strip():
        raise ValueError("Service type is required.")

    if not description.strip():
        raise ValueError("Job description is required.")

    if requested_bid_count < 1:
        raise ValueError("At least one requested bid is required.")

    lead_id = f"EMG-{secrets.token_hex(5).upper()}"

    query = """
        INSERT INTO leads (
            lead_id,
            zip_code,
            service_type,
            description,
            budget,
            urgency,
            requested_bid_count,
            customer_name,
            customer_email,
            customer_phone
        )
        VALUES (
            %s, %s, %s, %s, %s,
            %s, %s, %s, %s, %s
        )
        RETURNING *
    """

    values = (
        lead_id,
        zip_code.strip(),
        service_type.strip(),
        description.strip(),
        budget,
        urgency,
        requested_bid_count,
        customer_name,
        customer_email,
        customer_phone,
    )

    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(query, values)
            lead = cursor.fetchone()

    return lead


def get_lead_by_lead_id(lead_id: str) -> dict[str, Any] | None:
    """Retrieve a lead by its public EMG Lead ID."""

    query = """
        SELECT *
        FROM leads
        WHERE lead_id = %s
    """

    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(query, (lead_id,))
            lead = cursor.fetchone()

    return lead