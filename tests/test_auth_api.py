
import secrets

import pytest
from fastapi.testclient import TestClient

import app.api.main as api
from app.database.auth import provision_contractor_account
from app.database.connection import get_connection


TEST_ORIGIN = "http://localhost:8000"
TEST_PASSWORD = "EMG-Test-Password-123!"


@pytest.fixture
def api_client(monkeypatch):
    monkeypatch.setenv("EMG_ALLOWED_ORIGIN", TEST_ORIGIN)
    monkeypatch.setenv("EMG_COOKIE_SECURE", "false")

    return TestClient(api.app)


@pytest.fixture
def active_contractor():
    suffix = secrets.token_hex(5).upper()
    contractor_id = f"EMGT-{suffix}"
    email = f"api-test-{suffix.lower()}@example.com"

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
                    "API Authentication Test Contractor",
                    email,
                    "83814",
                    "Plumbing",
                ),
            )
            contractor = cursor.fetchone()

    try:
        provision_contractor_account(
            contractor_id=contractor_id,
            email=email,
            password=TEST_PASSWORD,
        )

        yield {
            "contractor_id": contractor_id,
            "email": email,
            "password": TEST_PASSWORD,
            "company_name": "API Authentication Test Contractor",
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


def test_contractor_identity_requires_login(api_client):
    response = api_client.get("/contractor-auth/me")

    assert response.status_code == 401


def test_contractor_can_log_in_and_receive_session_cookie(
    api_client,
    active_contractor,
):
    contractor = active_contractor

    response = api_client.post(
        "/contractor-auth/login",
        headers={"Origin": TEST_ORIGIN},
        json={
            "email": contractor["email"],
            "password": contractor["password"],
        },
    )

    assert response.status_code == 200
    assert response.json()["contractor_id"] == contractor["contractor_id"]
    assert response.json()["email"] == contractor["email"]
    assert response.cookies.get("emg_session")

    set_cookie = response.headers.get("set-cookie", "").lower()
    assert "httponly" in set_cookie


def test_logged_in_contractor_can_view_own_identity(
    api_client,
    active_contractor,
):
    contractor = active_contractor

    login = api_client.post(
        "/contractor-auth/login",
        headers={"Origin": TEST_ORIGIN},
        json={
            "email": contractor["email"],
            "password": contractor["password"],
        },
    )

    assert login.status_code == 200

    response = api_client.get("/contractor-auth/me")

    assert response.status_code == 200
    assert response.json() == {
        "contractor_id": contractor["contractor_id"],
        "company_name": contractor["company_name"],
        "email": contractor["email"],
    }


def test_logout_invalidates_contractor_session(
    api_client,
    active_contractor,
):
    contractor = active_contractor

    login = api_client.post(
        "/contractor-auth/login",
        headers={"Origin": TEST_ORIGIN},
        json={
            "email": contractor["email"],
            "password": contractor["password"],
        },
    )

    assert login.status_code == 200
    assert api_client.get("/contractor-auth/me").status_code == 200

    logout = api_client.post(
        "/contractor-auth/logout",
        headers={"Origin": TEST_ORIGIN},
    )

    assert logout.status_code == 200
    assert api_client.get("/contractor-auth/me").status_code == 401


def test_login_rejects_unapproved_origin(
    api_client,
    active_contractor,
):
    contractor = active_contractor

    response = api_client.post(
        "/contractor-auth/login",
        headers={"Origin": "https://unapproved.example"},
        json={
            "email": contractor["email"],
            "password": contractor["password"],
        },
    )

    assert response.status_code == 403
    assert api_client.cookies.get("emg_session") is None


def test_login_rejects_incorrect_password(
    api_client,
    active_contractor,
):
    contractor = active_contractor

    response = api_client.post(
        "/contractor-auth/login",
        headers={"Origin": TEST_ORIGIN},
        json={
            "email": contractor["email"],
            "password": "Definitely-Wrong-Password!",
        },
    )

    assert response.status_code == 401
    assert api_client.cookies.get("emg_session") is None

def test_login_returns_429_when_rate_limited(
    api_client,
    active_contractor,
    monkeypatch,
):
    contractor = active_contractor

    monkeypatch.setattr(
        api,
        "is_login_rate_limited",
        lambda **kwargs: True,
    )

    response = api_client.post(
        "/contractor-auth/login",
        headers={"Origin": TEST_ORIGIN},
        json={
            "email": contractor["email"],
            "password": contractor["password"],
        },
    )

    assert response.status_code == 429
    assert response.headers["retry-after"] == "900"
    assert api_client.cookies.get("emg_session") is None


def test_successful_login_does_not_record_failure(
    api_client,
    active_contractor,
    monkeypatch,
):
    contractor = active_contractor

    monkeypatch.setattr(
        api,
        "is_login_rate_limited",
        lambda **kwargs: False,
    )

    def unexpected_failure_record(**kwargs):
        raise AssertionError(
            "A successful login must not record a failure."
        )

    monkeypatch.setattr(
        api,
        "record_login_failure",
        unexpected_failure_record,
    )

    response = api_client.post(
        "/contractor-auth/login",
        headers={"Origin": TEST_ORIGIN},
        json={
            "email": contractor["email"],
            "password": contractor["password"],
        },
    )

    assert response.status_code == 200
    assert response.cookies.get("emg_session")