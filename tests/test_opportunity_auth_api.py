import secrets

import pytest
from fastapi.testclient import TestClient

import app.api.main as api
from app.database.auth import provision_contractor_account
from app.database.connection import get_connection
from app.database.contractors import create_contractor
from app.database.leads import create_lead
from app.services.matching import route_opportunities_to_fill_slots


TEST_ORIGIN = "http://localhost:8000"
TEST_PASSWORD = "EMG-Opportunity-Test-123!"


@pytest.fixture
def api_client(monkeypatch):
    monkeypatch.setenv("EMG_ALLOWED_ORIGIN", TEST_ORIGIN)
    monkeypatch.setenv("EMG_COOKIE_SECURE", "false")
    return TestClient(api.app)


@pytest.fixture
def opportunity_owned_by_contractor():
    """Create two contractors and an opportunity owned by Contractor A."""
    suffix = secrets.token_hex(5).upper()
    lead_id = None
    contractor_ids = []

    try:
        lead = create_lead(
            zip_code="83814",
            service_type="Roofing",
            description="Temporary opportunity authorization test.",
            requested_bid_count=1,
            customer_name="Temporary Authorization Test",
            customer_email=f"auth-test-{suffix.lower()}@example.com",
        )
        lead_id = lead["lead_id"]

        contractors = []

        for label in ("A", "B"):
            email = (
                f"auth-test-{label.lower()}-{suffix.lower()}@example.com"
            )
            contractor = create_contractor(
                company_name=f"EMG Auth Test {label} {suffix}",
                email=email,
                service_area=["83814"],
                specialties=["Roofing"],
            )
            contractors.append(contractor)
            contractor_ids.append(contractor["contractor_id"])

            provision_contractor_account(
                contractor_id=contractor["contractor_id"],
                email=email,
                password=TEST_PASSWORD,
            )

        opportunities = route_opportunities_to_fill_slots(lead_id)

        opportunity_a = next(
            (
                opportunity
                for opportunity in opportunities
                if opportunity["contractor_id"]
                == contractors[0]["contractor_id"]
            ),
            None,
        )

        if opportunity_a is None:
            raise AssertionError(
                "Routing did not create an opportunity for Contractor A."
            )

        yield {
            "contractor_a": {
                "contractor_id": contractors[0]["contractor_id"],
                "email": (
                    f"auth-test-a-{suffix.lower()}@example.com"
                ),
            },
            "contractor_b": {
                "contractor_id": contractors[1]["contractor_id"],
                "email": (
                    f"auth-test-b-{suffix.lower()}@example.com"
                ),
            },
            "opportunity_id": opportunity_a["opportunity_id"],
        }

    finally:
        with get_connection() as connection:
            with connection.cursor() as cursor:
                if contractor_ids:
                    cursor.execute(
                        """
                        DELETE FROM contractor_sessions
                        WHERE account_id IN (
                            SELECT id
                            FROM contractor_accounts
                            WHERE contractor_id IN (
                                SELECT id
                                FROM contractors
                                WHERE contractor_id = ANY(%s)
                            )
                        )
                        """,
                        (contractor_ids,),
                    )

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
                        DELETE FROM contractor_accounts
                        WHERE contractor_id IN (
                            SELECT id
                            FROM contractors
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


def login_contractor(api_client, contractor):
    response = api_client.post(
        "/contractor-auth/login",
        headers={"Origin": TEST_ORIGIN},
        json={
            "email": contractor["email"],
            "password": TEST_PASSWORD,
        },
    )
    assert response.status_code == 200, response.text
    return response


@pytest.mark.parametrize("decision", ["accept", "decline"])
def test_contractor_cannot_respond_to_another_contractors_opportunity(
    api_client,
    opportunity_owned_by_contractor,
    monkeypatch,
    decision,
):
    data = opportunity_owned_by_contractor

    # Authenticate as Contractor B.
    login_contractor(api_client, data["contractor_b"])

    service_calls = []

    def unexpected_service_call(*args, **kwargs):
        service_calls.append((args, kwargs))
        raise AssertionError(
            "The service must not run for another contractor's opportunity."
        )

    monkeypatch.setattr(
        api,
        "respond_to_opportunity",
        unexpected_service_call,
    )

    response = api_client.post(
        f"/opportunities/{data['opportunity_id']}/respond",
        headers={"Origin": TEST_ORIGIN},
        json={"decision": decision},
    )

    assert response.status_code == 404, response.text
    assert response.json()["detail"] == "Opportunity not found."
    assert service_calls == []

    # Confirm the opportunity was not changed.
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT status
                FROM opportunities
                WHERE opportunity_id = %s
                """,
                (data["opportunity_id"],),
            )
            opportunity = cursor.fetchone()

    assert opportunity is not None
    assert opportunity["status"] == "PENDING"


def test_unauthenticated_contractor_cannot_respond_to_opportunity(
    api_client,
    opportunity_owned_by_contractor,
    monkeypatch,
):
    data = opportunity_owned_by_contractor
    api_client.cookies.clear()

    service_calls = []

    def unexpected_service_call(*args, **kwargs):
        service_calls.append((args, kwargs))
        raise AssertionError(
            "The service must not run without authentication."
        )

    monkeypatch.setattr(
        api,
        "respond_to_opportunity",
        unexpected_service_call,
    )

    response = api_client.post(
        f"/opportunities/{data['opportunity_id']}/respond",
        headers={"Origin": TEST_ORIGIN},
        json={"decision": "decline"},
    )

    assert response.status_code == 401, response.text
    assert service_calls == []

def test_contractor_can_respond_to_own_opportunity(
    api_client,
    opportunity_owned_by_contractor,
):
    data = opportunity_owned_by_contractor

    login_contractor(api_client, data["contractor_a"])

    response = api_client.post(
        f"/opportunities/{data['opportunity_id']}/respond",
        headers={"Origin": TEST_ORIGIN},
        json={"decision": "decline"},
    )

    assert response.status_code == 200, response.text

    result = response.json()
    assert result["opportunity_id"] == data["opportunity_id"]
    assert result["decision"] == "decline"
    assert result["status"] == "DECLINED"

    # The API must not expose other contractors' invitation details.
    assert result["next_opportunities"] == []


def test_contractor_inbox_requires_authentication(
    api_client,
    opportunity_owned_by_contractor,
):
    api_client.cookies.clear()

    response = api_client.get("/contractor/opportunities")

    assert response.status_code == 401, response.text


def test_contractor_inbox_only_returns_own_opportunities(
    api_client,
    opportunity_owned_by_contractor,
):
    data = opportunity_owned_by_contractor

    # Contractor B is authenticated but does not own Contractor A's invitation.
    login_contractor(api_client, data["contractor_b"])
    response = api_client.get("/contractor/opportunities")

    assert response.status_code == 200, response.text
    assert response.json() == []

    # Contractor A should see the invitation assigned to them.
    api_client.cookies.clear()
    login_contractor(api_client, data["contractor_a"])
    response = api_client.get("/contractor/opportunities")

    assert response.status_code == 200, response.text
    opportunities = response.json()

    assert len(opportunities) == 1
    assert opportunities[0]["opportunity_id"] == data["opportunity_id"]
    assert opportunities[0]["status"] == "PENDING"


def test_contractor_inbox_does_not_expose_homeowner_contacts_or_photo_keys(
    api_client,
    opportunity_owned_by_contractor,
):
    data = opportunity_owned_by_contractor
    login_contractor(api_client, data["contractor_a"])

    response = api_client.get("/contractor/opportunities")

    assert response.status_code == 200, response.text
    opportunities = response.json()
    assert len(opportunities) == 1

    item = opportunities[0]
    assert item["zip_code"] == "83814"
    assert item["service_type"] == "Roofing"
    assert item["description"]
    assert item["sent_at"]
    assert "expires_at" in item

    forbidden_fields = {
        "customer_name",
        "customer_email",
        "customer_phone",
        "s3_key",
        "photos",
    }
    assert forbidden_fields.isdisjoint(item.keys())
