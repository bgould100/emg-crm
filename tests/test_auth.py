import secrets
from unittest.mock import patch
import pytest

from app.database.auth import (
    authenticate_contractor_session,
    change_contractor_password,
    complete_required_password_change,
    login_contractor,
    logout_contractor,
    provision_contractor_account,
    reset_contractor_password as reset_contractor_portal_password,
)
from app.database.connection import get_connection
from scripts.reset_contractor_password import reset_contractor_password


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


def test_password_change_updates_password_and_revokes_sessions(active_contractor):
    contractor = active_contractor
    old_password = "EMG-Test-Password-123!"
    new_password = "EMG-New-Password-456!"

    provision_contractor_account(
        contractor_id=contractor["contractor_id"],
        email=contractor["email"],
        password=old_password,
    )

    first_login = login_contractor(
        email=contractor["email"],
        password=old_password,
    )
    second_login = login_contractor(
        email=contractor["email"],
        password=old_password,
    )

    change_contractor_password(
        account_id=first_login["account_id"],
        current_password=old_password,
        new_password=new_password,
    )

    assert authenticate_contractor_session(first_login["session_token"]) is None
    assert authenticate_contractor_session(second_login["session_token"]) is None

    with pytest.raises(ValueError, match="Invalid email or password"):
        login_contractor(
            email=contractor["email"],
            password=old_password,
        )

    new_login = login_contractor(
        email=contractor["email"],
        password=new_password,
    )
    assert new_login["account_id"] == first_login["account_id"]


def test_password_change_rejects_wrong_current_password(active_contractor):
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

    with pytest.raises(ValueError, match="Current password is incorrect"):
        change_contractor_password(
            account_id=login["account_id"],
            current_password="Wrong-Current-Password!",
            new_password="EMG-New-Password-456!",
        )

    assert authenticate_contractor_session(login["session_token"]) is not None


def test_manual_password_reset_changes_password_and_revokes_sessions(
    active_contractor,
):
    contractor = active_contractor
    old_password = "EMG-Test-Password-123!"

    provision_contractor_account(
        contractor_id=contractor["contractor_id"],
        email=contractor["email"],
        password=old_password,
    )

    first_login = login_contractor(
        email=contractor["email"],
        password=old_password,
    )
    second_login = login_contractor(
        email=contractor["email"],
        password=old_password,
    )

    with patch(
        "builtins.input",
        return_value="Authentication Test Contractor",
    ):
        temporary_password = reset_contractor_password(
            contractor["email"].upper(),
        )

    assert len(temporary_password) == 32
    assert authenticate_contractor_session(
        first_login["session_token"]
    ) is None
    assert authenticate_contractor_session(
        second_login["session_token"]
    ) is None

    with pytest.raises(ValueError, match="Invalid email or password"):
        login_contractor(
            email=contractor["email"],
            password=old_password,
        )

    new_login = login_contractor(
        email=contractor["email"],
        password=temporary_password,
    )
    assert new_login["account_id"] == first_login["account_id"]


def test_admin_password_reset_requires_new_password_and_revokes_sessions(
    active_contractor,
):
    contractor = active_contractor
    old_password = "EMG-Test-Password-123!"
    temporary_password = "EMG-Temporary-Password-456!"

    provision_contractor_account(
        contractor_id=contractor["contractor_id"],
        email=contractor["email"],
        password=old_password,
    )

    first_login = login_contractor(
        email=contractor["email"],
        password=old_password,
    )
    second_login = login_contractor(
        email=contractor["email"],
        password=old_password,
    )

    reset_contractor_portal_password(
        contractor_id=contractor["contractor_id"],
        temporary_password=temporary_password,
    )

    assert authenticate_contractor_session(first_login["session_token"]) is None
    assert authenticate_contractor_session(second_login["session_token"]) is None

    with pytest.raises(ValueError, match="Invalid email or password"):
        login_contractor(
            email=contractor["email"],
            password=old_password,
        )

    temporary_login = login_contractor(
        email=contractor["email"],
        password=temporary_password,
    )
    assert temporary_login["must_change_password"] is True


def test_required_password_change_replaces_temporary_password(
    active_contractor,
):
    contractor = active_contractor
    old_password = "EMG-Test-Password-123!"
    temporary_password = "EMG-Temporary-Password-456!"
    new_password = "EMG-Permanent-Password-789!"

    provision_contractor_account(
        contractor_id=contractor["contractor_id"],
        email=contractor["email"],
        password=old_password,
    )

    reset_contractor_portal_password(
        contractor_id=contractor["contractor_id"],
        temporary_password=temporary_password,
    )

    temporary_login = login_contractor(
        email=contractor["email"],
        password=temporary_password,
    )

    result = complete_required_password_change(
        account_id=temporary_login["account_id"],
        session_token=temporary_login["session_token"],
        new_password=new_password,
    )

    assert result["session_token"]
    assert authenticate_contractor_session(
        temporary_login["session_token"]
    ) is None

    new_session = authenticate_contractor_session(result["session_token"])
    assert new_session is not None
    assert new_session["must_change_password"] is False

    with pytest.raises(ValueError, match="Invalid email or password"):
        login_contractor(
            email=contractor["email"],
            password=temporary_password,
        )

    permanent_login = login_contractor(
        email=contractor["email"],
        password=new_password,
    )
    assert permanent_login["must_change_password"] is False
