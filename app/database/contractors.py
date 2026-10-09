
import secrets
from typing import Any

from app.database.connection import get_connection


def create_contractor(
    *,
    company_name: str,
    email: str,
    service_area: list[str],
    specialties: list[str],
    contact_name: str | None = None,
    phone: str | None = None,
) -> dict[str, Any]:
    """Register a contractor in the EMG database."""

    if not company_name.strip():
        raise ValueError("Company name is required.")

    if not email.strip():
        raise ValueError("Contractor email is required.")

    if not service_area or not any(area.strip() for area in service_area):
        raise ValueError("At least one service ZIP code is required.")

    if not specialties or not any(item.strip() for item in specialties):
        raise ValueError("At least one specialty is required.")

    contractor_id = f"EMGC-{secrets.token_hex(5).upper()}"

    query = """
        INSERT INTO contractors (
            contractor_id,
            company_name,
            contact_name,
            email,
            phone,
            service_area,
            specialties,
            availability
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, 'AVAILABLE')
        RETURNING *
    """

    values = (
        contractor_id,
        company_name.strip(),
        contact_name.strip() if contact_name else None,
        email.strip().lower(),
        phone,
        ",".join(area.strip() for area in service_area if area.strip()),
        ",".join(item.strip() for item in specialties if item.strip()),
    )

    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(query, values)
            contractor = cursor.fetchone()

    return contractor


def list_available_contractors() -> list[dict[str, Any]]:
    """Return active contractors marked available for matching."""

    query = """
        SELECT
            contractor_id,
            company_name,
            service_area,
            specialties,
            availability
        FROM contractors
        WHERE status = 'ACTIVE'
          AND availability = 'AVAILABLE'
        ORDER BY company_name
    """

    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(query)
            return list(cursor.fetchall())