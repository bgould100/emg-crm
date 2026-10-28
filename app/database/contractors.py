
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
        VALUES (%s, %s, %s::varchar, %s, %s, %s, %s, 'AVAILABLE')
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

            cursor.execute(
                """
                INSERT INTO status_history (
                    contractor_id,
                    event_type,
                    new_status,
                    metadata
                )
                VALUES (
                    %s,
                    'ContractorCreated',
                    %s,
                    jsonb_build_object('company_name', %s::text)
                )
                """,
                (
                    contractor["id"],
                    contractor["status"],
                    contractor["company_name"],
                ),
            )

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


def update_contractor_matching_profile(
    *,
    contractor_pk: int,
    service_area: list[str],
    specialties: list[str],
) -> dict[str, Any]:
    """Update matching fields without changing contractor status."""

    normalized_areas = list(
        dict.fromkeys(area.strip() for area in service_area if area.strip())
    )
    normalized_specialties = list(
        dict.fromkeys(item.strip() for item in specialties if item.strip())
    )

    if not normalized_areas:
        raise ValueError("Enter at least one service ZIP code.")

    if not normalized_specialties:
        raise ValueError("Select at least one service specialty.")

    # US ZIP codes must be exactly five digits. Keep them as strings.
    invalid_zips = [
        area for area in normalized_areas
        if len(area) != 5 or not area.isdigit()
    ]
    if invalid_zips:
        raise ValueError(
            "Each service ZIP code must contain exactly five digits."
        )

    if any(len(item) > 100 for item in normalized_specialties):
        raise ValueError("Each specialty must be 100 characters or fewer.")

    query = """
        UPDATE contractors
        SET service_area = %s,
            specialties = %s,
            updated_at = CURRENT_TIMESTAMP
        WHERE id = %s
        RETURNING contractor_id, company_name, service_area,
                  specialties, availability, status
    """



    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                query,
                (
                    ",".join(normalized_areas),
                    ",".join(normalized_specialties),
                    contractor_pk,
                ),
            )
            contractor = cursor.fetchone()

            if contractor is None:
                raise LookupError("Contractor profile not found.")

            cursor.execute(
                """
                INSERT INTO status_history (
                    contractor_id,
                    event_type,
                    metadata
                )
                VALUES (
                    %s,
                    'ContractorMatchingProfileUpdated',
                    jsonb_build_object(
                        'service_area', %s,
                        'specialties', %s
                    )
                )
                """,
                (
                    contractor_pk,
                    contractor["service_area"],
                    contractor["specialties"],
                ),
            )

    return contractor