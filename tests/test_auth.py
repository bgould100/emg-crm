
import secrets

import pytest

from app.database.auth import (
    authenticate_contractor_session,
    login_contractor,
    logout_contractor,
    provision_contractor_account,
)
from app.database.connection import get_connection


@pytest.fixture
def active_contractor():
    suffix = secrets.token_hex(5).upper()
    contractor_id = f"EMGT-{suffix}"
    email = f"auth-test-{suffix.lower()}@example.com"

    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO contractors (
                    contractor_id,
                    company_name,
                    email,
                    service_area,
                    specialties
                )
                VALUES (%s, %s, %s, %s, %s)
                RETURNING id
                """,
                (
                    contractor_id,
                    "Authentication Test Contractor",
                    email,
                    "83814",
                    "Plumbing",
                ),
            )
            contractor = cursor.fetchone()

    try:
        yield {
            "contractor_id": contractor_id,
            "email": email,
            "internal_id": contractor["id"],
        }
    finally:
        with get_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    DELETE FROM contractor_sessions
                    WHERE account_id IN (
                        SELECT id
                        FROM contractor_accounts
                        WHERE contractor_id = %s
                    )
                    """,
                    (contractor["id"],),
                )
                cursor.execute(
                    """
                    DELETE FROM contractor_accounts
                    WHERE contractor_id = %s
                    """,
                    (contractor["id"],),
                )
                cursor.execute(
                    "DELETE FROM contractors WHERE id = %s",
                    (contractor["id"],),
                )


def test_login_session_and_logout(active_contractor):
    contractor = active_contractor
    password = "EMG-Test-Password-123!"

    provision_contractor_account(
        contractor_id=contractor["contractor_id"],
        email=contractor["email"],
        password=password,
    )

    login = login_contractor(
        email=contractor["email"].upper(),
        password=password,
    )

    assert login["contractor_pk"] == contractor["internal_id"]
    assert login["email"] == contractor["email"]
    assert login["session_token"]

    session = authenticate_contractor_session(login["session_token"])
    assert session is not None
    assert session["contractor_pk"] == contractor["internal_id"]

    logout_contractor(login["session_token"])

    assert authenticate_contractor_session(login["session_token"]) is None


def test_wrong_password_is_rejected(active_contractor):
    contractor = active_contractor

    provision_contractor_account(
        contractor_id=contractor["contractor_id"],
        email=contractor["email"],
        password="EMG-Test-Password-123!",
    )

    with pytest.raises(ValueError, match="Invalid email or password"):
        login_contractor(
            email=contractor["email"],
            password="This-Is-The-Wrong-Password!",
        )


def test_disabled_contractor_cannot_log_in(active_contractor):
    contractor = active_contractor
    password = "EMG-Test-Password-123!"

    provision_contractor_account(
        contractor_id=contractor["contractor_id"],
        email=contractor["email"],
        password=password,
    )

    login = login_contractor(
        email=contractor["email"],
        password=password,
    )

    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE contractors
                SET status = 'DISABLED'
                WHERE id = %s
                """,
                (contractor["internal_id"],),
            )

    with pytest.raises(ValueError, match="Invalid email or password"):
        login_contractor(
            email=contractor["email"],
            password=password,
        )

    assert authenticate_contractor_session(login["session_token"]) is None


def test_account_provisioning_rejects_email_mismatch(active_contractor):
    contractor = active_contractor

    with pytest.raises(ValueError, match="must match"):
        provision_contractor_account(
            contractor_id=contractor["contractor_id"],
            email="different@example.com",
            password="EMG-Test-Password-123!",
        )


def test_account_provisioning_rejects_short_password(active_contractor):
    contractor = active_contractor

    with pytest.raises(ValueError, match="at least 12 characters"):
        provision_contractor_account(
            contractor_id=contractor["contractor_id"],
            email=contractor["email"],
            password="short",
        )
