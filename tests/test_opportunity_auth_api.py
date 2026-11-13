import secrets
import io
from PIL import Image

import pytest
from fastapi.testclient import TestClient
from datetime import timedelta
from urllib.parse import parse_qs, urlparse
from botocore.exceptions import ClientError

from app.services import homeowner_review_delivery
from app.services.review_tokens import review_token_matches

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
    test_specialty = f"Auth API Roofing {suffix}"
    lead_id = None
    contractor_ids = []

    try:
        lead = create_lead(
            zip_code="83814",
            service_type=test_specialty,
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
                specialties=[test_specialty],
            )
            contractors.append(contractor)
            contractor_ids.append(contractor["contractor_id"])

            provision_contractor_account(
                contractor_id=contractor["contractor_id"],
                email=email,
                password=TEST_PASSWORD,
            )
        with get_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT
                        c.contractor_id,
                        c.status,
                        c.availability,
                        c.default_referral_fee_rate,
                        c.service_area,
                        c.specialties,
                        l.zip_code,
                        l.service_type
                    FROM contractors c
                    CROSS JOIN leads l
                    WHERE l.lead_id = %s
                    AND c.contractor_id = ANY(%s)
                    """,
                    (lead_id, contractor_ids),
                )
                print("ROUTING DIAGNOSTIC:", cursor.fetchall())
        with get_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT opportunity_id, contractor_id, status, slot_released
                    FROM opportunities
                    WHERE lead_id = (
                        SELECT id FROM leads WHERE lead_id = %s
                    )
                    """,
                    (lead_id,),
                )
                print("EXISTING OPPORTUNITIES:", cursor.fetchall())
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
            "lead_id": lead_id,
            "service_type": test_specialty,
            "contractor_a": {
                "contractor_id": contractors[0]["contractor_id"],
                "email": f"auth-test-a-{suffix.lower()}@example.com",
            },
            "contractor_b": {
                "contractor_id": contractors[1]["contractor_id"],
                "email": f"auth-test-b-{suffix.lower()}@example.com",
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
                preserve_financial_records = False
                if lead_id:
                    cursor.execute(
                        """
                        SELECT EXISTS (
                            SELECT 1
                            FROM jobs j
                            JOIN job_fee_accruals a ON a.job_id = j.id
                            WHERE j.lead_id = (
                                SELECT id FROM leads WHERE lead_id = %s
                            )
                        ) AS has_accrual
                        """,
                        (lead_id,),
                    )
                    preserve_financial_records = cursor.fetchone()["has_accrual"]
                if lead_id and not preserve_financial_records:
                    cursor.execute(
                        "SELECT id FROM leads WHERE lead_id = %s",
                        (lead_id,),
                    )
                    lead_row = cursor.fetchone()

                    if lead_row:
                        db_lead_id = lead_row["id"]

                        # Remove only alerts belonging to this test lead.
                        cursor.execute(
                            """
                            DELETE FROM outbox_events
                            WHERE payload->>'lead_id' = %s
                            """,
                            (lead_id,),
                        )

                        cursor.execute(
                            "DELETE FROM status_history WHERE lead_id = %s",
                            (db_lead_id,),
                        )

                        cursor.execute(
                            "DELETE FROM photos WHERE lead_id = %s",
                            (db_lead_id,),
                        )

                        cursor.execute(
                            """
                            DELETE FROM homeowner_review_delivery_events
                            WHERE invitation_id IN (
                                SELECT id
                                FROM homeowner_review_invitations
                                WHERE job_id IN (
                                    SELECT id FROM jobs WHERE lead_id = %s
                                )
                            )
                            """,
                            (db_lead_id,),
                        )

                        cursor.execute(
                            """
                            DELETE FROM job_financial_audit_events
                            WHERE job_id IN (
                                SELECT id FROM jobs WHERE lead_id = %s
                            )
                            """,
                            (db_lead_id,),
                        )

                        cursor.execute(
                            """
                            DELETE FROM homeowner_review_invitations
                            WHERE job_id IN (
                                SELECT id FROM jobs WHERE lead_id = %s
                            )
                            """,
                            (db_lead_id,),
                        )

                        cursor.execute(
                            """
                            DELETE FROM homeowner_review_discrepancies
                            WHERE job_id IN (
                                SELECT id FROM jobs WHERE lead_id = %s
                            )
                            """,
                            (db_lead_id,),
                        )

                        cursor.execute(
                            """
                            DELETE FROM job_installments
                            WHERE job_id IN (
                                SELECT id FROM jobs WHERE lead_id = %s
                            )
                            """,
                            (db_lead_id,),
                        )

                        cursor.execute(
                            """
                            DELETE FROM job_payment_agreements
                            WHERE job_id IN (
                                SELECT id FROM jobs WHERE lead_id = %s
                            )
                            """,
                            (db_lead_id,),
                        )

                        cursor.execute(
                            """
                            DELETE FROM job_collections
                            WHERE job_id IN (
                                SELECT id FROM jobs WHERE lead_id = %s
                            )
                            """,
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

                if contractor_ids and not preserve_financial_records:
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


def login_contractor(api_client, contractor):
    password = contractor.get("password", TEST_PASSWORD)

    response = api_client.post(
        "/contractor-auth/login",
        headers={"Origin": TEST_ORIGIN},
        json={
            "email": contractor["email"],
            "password": password,
        },
    )
    assert response.status_code == 200, response.text

    if response.json().get("must_change_password"):
        permanent_password = "EMG-Opportunity-Permanent-456!"

        change_response = api_client.post(
            "/contractor-auth/required-password-change",
            headers={"Origin": TEST_ORIGIN},
            json={
                "new_password": permanent_password,
                "confirm_password": permanent_password,
            },
        )
        assert change_response.status_code == 200, change_response.text

        # Remember the permanent password for subsequent logins in this test.
        contractor["password"] = permanent_password

    return response


@pytest.mark.parametrize("decision", ["accept", "decline"])
def test_contractor_cannot_respond_to_another_contractors_opportunity(
    api_client,
    opportunity_owned_by_contractor,
    monkeypatch,
    decision,
):
    data = opportunity_owned_by_contractor
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
    assert result["next_opportunities"] == []


def test_contractor_inbox_requires_authentication(
    api_client,
    opportunity_owned_by_contractor,
):
    api_client.cookies.clear()

    response = api_client.get(
        "/contractor/opportunities",
        headers={"Origin": TEST_ORIGIN},
    )

    assert response.status_code == 401, response.text


def test_contractor_inbox_only_returns_own_opportunities(
    api_client,
    opportunity_owned_by_contractor,
):
    data = opportunity_owned_by_contractor

    login_contractor(api_client, data["contractor_b"])
    response = api_client.get(
        "/contractor/opportunities",
        headers={"Origin": TEST_ORIGIN},
    )

    assert response.status_code == 200, response.text
    assert response.json() == []

    api_client.cookies.clear()
    login_contractor(api_client, data["contractor_a"])
    response = api_client.get(
        "/contractor/opportunities",
        headers={"Origin": TEST_ORIGIN},
    )

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

    response = api_client.get(
        "/contractor/opportunities",
        headers={"Origin": TEST_ORIGIN},
    )

    assert response.status_code == 200, response.text
    opportunities = response.json()
    assert len(opportunities) == 1

    item = opportunities[0]
    assert item["zip_code"] == "83814"
    assert item["service_type"] == data["service_type"]
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


def test_contractor_opportunity_detail_requires_authentication(
    api_client,
    opportunity_owned_by_contractor,
):
    data = opportunity_owned_by_contractor
    api_client.cookies.clear()

    response = api_client.get(
        f"/contractor/opportunities/{data['opportunity_id']}",
        headers={"Origin": TEST_ORIGIN},
    )

    assert response.status_code == 401, response.text


def test_contractor_can_view_own_opportunity_details(
    api_client,
    opportunity_owned_by_contractor,
):
    data = opportunity_owned_by_contractor
    login_contractor(api_client, data["contractor_a"])

    response = api_client.get(
        f"/contractor/opportunities/{data['opportunity_id']}",
        headers={"Origin": TEST_ORIGIN},
    )

    assert response.status_code == 200, response.text
    detail = response.json()

    assert detail["opportunity_id"] == data["opportunity_id"]
    assert detail["status"] == "PENDING"
    assert detail["zip_code"] == "83814"
    assert detail["service_type"] == opportunity_owned_by_contractor["service_type"]
    assert detail["description"]
    assert detail["sent_at"]
    assert "expires_at" in detail
    assert "expiration_paused" in detail

    forbidden_fields = {
        "customer_name",
        "customer_email",
        "customer_phone",
        "s3_key",
        "photos",
    }
    assert forbidden_fields.isdisjoint(detail.keys())


def test_contractor_cannot_view_another_contractors_opportunity_details(
    api_client,
    opportunity_owned_by_contractor,
):
    data = opportunity_owned_by_contractor
    login_contractor(api_client, data["contractor_b"])

    response = api_client.get(
        f"/contractor/opportunities/{data['opportunity_id']}",
        headers={"Origin": TEST_ORIGIN},
    )

    assert response.status_code == 404, response.text


def test_contractor_job_list_requires_authentication(
    api_client,
    opportunity_owned_by_contractor,
):
    api_client.cookies.clear()

    response = api_client.get(
        "/contractor/jobs",
        headers={"Origin": TEST_ORIGIN},
    )

    assert response.status_code == 401, response.text


def test_contractor_job_list_only_returns_own_jobs(
    api_client,
    opportunity_owned_by_contractor,
):
    data = opportunity_owned_by_contractor

    login_contractor(api_client, data["contractor_a"])
    accept_response = api_client.post(
        f"/opportunities/{data['opportunity_id']}/respond",
        headers={"Origin": TEST_ORIGIN},
        json={"decision": "accept"},
    )
    assert accept_response.status_code == 200, accept_response.text

    response_a = api_client.get(
        "/contractor/jobs",
        headers={"Origin": TEST_ORIGIN},
    )
    assert response_a.status_code == 200, response_a.text
    jobs_a = response_a.json()

    assert len(jobs_a) == 1
    assert jobs_a[0]["job_id"] == accept_response.json()["job_id"]
    assert jobs_a[0]["status"] == "QUOTE_IN_PROGRESS"

    api_client.cookies.clear()
    login_contractor(api_client, data["contractor_b"])

    response_b = api_client.get(
        "/contractor/jobs",
        headers={"Origin": TEST_ORIGIN},
    )
    assert response_b.status_code == 200, response_b.text
    assert response_b.json() == []


def test_contractor_job_list_does_not_expose_homeowner_contacts(
    api_client,
    opportunity_owned_by_contractor,
):
    data = opportunity_owned_by_contractor
    login_contractor(api_client, data["contractor_a"])

    accept_response = api_client.post(
        f"/opportunities/{data['opportunity_id']}/respond",
        headers={"Origin": TEST_ORIGIN},
        json={"decision": "accept"},
    )
    assert accept_response.status_code == 200, accept_response.text

    response = api_client.get(
        "/contractor/jobs",
        headers={"Origin": TEST_ORIGIN},
    )
    assert response.status_code == 200, response.text

    jobs = response.json()
    assert len(jobs) == 1

    job = jobs[0]
    assert "customer_name" not in job
    assert "customer_email" not in job
    assert "customer_phone" not in job

def test_contractor_can_view_homeowner_contacts_for_own_accepted_job(
    api_client,
    opportunity_owned_by_contractor,
):
    data = opportunity_owned_by_contractor
    login_contractor(api_client, data["contractor_a"])

    accept_response = api_client.post(
        f"/opportunities/{data['opportunity_id']}/respond",
        headers={"Origin": TEST_ORIGIN},
        json={"decision": "accept"},
    )
    assert accept_response.status_code == 200, accept_response.text
    job_id = accept_response.json()["job_id"]

    response = api_client.get(
        f"/contractor/jobs/{job_id}",
        headers={"Origin": TEST_ORIGIN},
    )

    assert response.status_code == 200, response.text
    job = response.json()
    assert job["job_id"] == job_id
    assert job["customer_name"] == "Temporary Authorization Test"
    assert job["customer_email"].startswith("auth-test-")
    assert job["customer_email"].endswith("@example.com")
    assert "customer_phone" in job


def test_contractor_cannot_view_another_contractors_job_contacts(
    api_client,
    opportunity_owned_by_contractor,
):
    data = opportunity_owned_by_contractor
    login_contractor(api_client, data["contractor_a"])

    accept_response = api_client.post(
        f"/opportunities/{data['opportunity_id']}/respond",
        headers={"Origin": TEST_ORIGIN},
        json={"decision": "accept"},
    )
    assert accept_response.status_code == 200, accept_response.text
    job_id = accept_response.json()["job_id"]

    api_client.cookies.clear()
    login_contractor(api_client, data["contractor_b"])

    response = api_client.get(
        f"/contractor/jobs/{job_id}",
        headers={"Origin": TEST_ORIGIN},
    )

    assert response.status_code == 404, response.text
    assert response.json()["detail"] == "Job not found."


def test_contractor_job_details_require_authentication(
    api_client,
    opportunity_owned_by_contractor,
):
    data = opportunity_owned_by_contractor
    login_contractor(api_client, data["contractor_a"])

    accept_response = api_client.post(
        f"/opportunities/{data['opportunity_id']}/respond",
        headers={"Origin": TEST_ORIGIN},
        json={"decision": "accept"},
    )
    assert accept_response.status_code == 200, accept_response.text
    job_id = accept_response.json()["job_id"]

    api_client.cookies.clear()

    response = api_client.get(
        f"/contractor/jobs/{job_id}",
        headers={"Origin": TEST_ORIGIN},
    )

    assert response.status_code == 401, response.text


def test_job_status_update_requires_authentication(
    api_client,
    opportunity_owned_by_contractor,
):
    data = opportunity_owned_by_contractor
    login_contractor(api_client, data["contractor_a"])

    accept_response = api_client.post(
        f"/opportunities/{data['opportunity_id']}/respond",
        headers={"Origin": TEST_ORIGIN},
        json={"decision": "accept"},
    )
    assert accept_response.status_code == 200, accept_response.text
    job_id = accept_response.json()["job_id"]

    api_client.cookies.clear()

    response = api_client.patch(
        f"/jobs/{job_id}/status",
        headers={"Origin": TEST_ORIGIN},
        json={"status": "BID_LOST"},
    )

    assert response.status_code == 401, response.text


def test_contractor_cannot_update_another_contractors_job(
    api_client,
    opportunity_owned_by_contractor,
):
    data = opportunity_owned_by_contractor
    login_contractor(api_client, data["contractor_a"])

    accept_response = api_client.post(
        f"/opportunities/{data['opportunity_id']}/respond",
        headers={"Origin": TEST_ORIGIN},
        json={"decision": "accept"},
    )
    assert accept_response.status_code == 200, accept_response.text
    job_id = accept_response.json()["job_id"]

    api_client.cookies.clear()
    login_contractor(api_client, data["contractor_b"])

    response = api_client.patch(
        f"/jobs/{job_id}/status",
        headers={"Origin": TEST_ORIGIN},
        json={"status": "BID_LOST"},
    )

    assert response.status_code == 404, response.text
    assert response.json()["detail"] == "Job not found."

    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT status FROM jobs WHERE job_id = %s",
                (job_id,),
            )
            job = cursor.fetchone()

    assert job is not None
    assert job["status"] == "QUOTE_IN_PROGRESS"


def test_contractor_can_update_own_job_status(
    api_client,
    opportunity_owned_by_contractor,
):
    data = opportunity_owned_by_contractor
    login_contractor(api_client, data["contractor_a"])

    accept_response = api_client.post(
        f"/opportunities/{data['opportunity_id']}/respond",
        headers={"Origin": TEST_ORIGIN},
        json={"decision": "accept"},
    )
    assert accept_response.status_code == 200, accept_response.text
    job_id = accept_response.json()["job_id"]

    response = api_client.patch(
        f"/jobs/{job_id}/status",
        headers={"Origin": TEST_ORIGIN},
        json={"status": "BID_LOST"},
    )

    assert response.status_code == 200, response.text
    result = response.json()
    assert result["job_id"] == job_id
    assert result["status"] == "BID_LOST"
    assert result["changed"] is True

    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT status FROM jobs WHERE job_id = %s",
                (job_id,),
            )
            job = cursor.fetchone()

    assert job is not None
    assert job["status"] == "BID_LOST"


def test_job_status_update_rejects_invalid_transition_and_is_idempotent(
    api_client,
    opportunity_owned_by_contractor,
):
    data = opportunity_owned_by_contractor
    login_contractor(api_client, data["contractor_a"])

    accept_response = api_client.post(
        f"/opportunities/{data['opportunity_id']}/respond",
        headers={"Origin": TEST_ORIGIN},
        json={"decision": "accept"},
    )
    assert accept_response.status_code == 200, accept_response.text
    job_id = accept_response.json()["job_id"]

    # A job cannot jump directly from a quote to completed.
    invalid_response = api_client.patch(
        f"/jobs/{job_id}/status",
        headers={"Origin": TEST_ORIGIN},
        json={"status": "JOB_COMPLETED"},
    )
    assert invalid_response.status_code == 409, invalid_response.text

    # A valid status change should succeed.
    change_response = api_client.patch(
        f"/jobs/{job_id}/status",
        headers={"Origin": TEST_ORIGIN},
        json={"status": "BID_LOST"},
    )
    assert change_response.status_code == 200, change_response.text

    change_result = change_response.json()
    assert change_result["status"] == "BID_LOST"
    assert change_result["changed"] is True

    # The actual transition should create exactly one history record.
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT
                    sh.event_type,
                    sh.previous_status,
                    sh.new_status
                FROM status_history AS sh
                JOIN jobs AS j ON j.id = sh.job_id
                WHERE j.job_id = %s
                  AND sh.event_type = 'JobStatusChanged'
                """,
                (job_id,),
            )
            history = cursor.fetchall()

    assert len(history) == 1
    assert history[0]["event_type"] == "JobStatusChanged"
    assert history[0]["previous_status"] == "QUOTE_IN_PROGRESS"
    assert history[0]["new_status"] == "BID_LOST"

    # Retrying the same request should not create another history record.
    repeated_response = api_client.patch(
        f"/jobs/{job_id}/status",
        headers={"Origin": TEST_ORIGIN},
        json={"status": "BID_LOST"},
    )
    assert repeated_response.status_code == 200, repeated_response.text

    repeated_result = repeated_response.json()
    assert repeated_result["status"] == "BID_LOST"
    assert repeated_result["changed"] is False

    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT COUNT(*) AS history_count
                FROM status_history AS sh
                JOIN jobs AS j ON j.id = sh.job_id
                WHERE j.job_id = %s
                  AND sh.event_type = 'JobStatusChanged'
                """,
                (job_id,),
            )
            history_count = cursor.fetchone()["history_count"]

    assert history_count == 1


def test_multiple_contractors_reporting_bid_won_queues_one_alert(
    api_client,
    opportunity_owned_by_contractor,
):
    data = opportunity_owned_by_contractor

    # Contractor A accepts the first opportunity.
    login_contractor(api_client, data["contractor_a"])

    accept_a = api_client.post(
        f"/opportunities/{data['opportunity_id']}/respond",
        headers={"Origin": TEST_ORIGIN},
        json={"decision": "accept"},
    )
    assert accept_a.status_code == 200, accept_a.text

    # Request a second bid slot for this test lead.
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE leads
                SET requested_bid_count = 2
                WHERE lead_id = %s
                """,
                (data["lead_id"],),
            )

    new_opportunities = route_opportunities_to_fill_slots(data["lead_id"])

    opportunity_b = next(
        (
            opportunity
            for opportunity in new_opportunities
            if opportunity["contractor_id"]
            == data["contractor_b"]["contractor_id"]
        ),
        None,
    )
    assert opportunity_b is not None, (
        "Routing did not create an opportunity for Contractor B."
    )

    # Contractor B accepts the second opportunity.
    login_contractor(api_client, data["contractor_b"])

    accept_b = api_client.post(
        f"/opportunities/{opportunity_b['opportunity_id']}/respond",
        headers={"Origin": TEST_ORIGIN},
        json={"decision": "accept"},
    )
    assert accept_b.status_code == 200, accept_b.text

    # The first reported win should not trigger a multiple-win alert.
    login_contractor(api_client, data["contractor_a"])

    win_a = api_client.patch(
        f"/jobs/{accept_a.json()['job_id']}/status",
        headers={"Origin": TEST_ORIGIN},
        json={"status": "BID_WON"},
    )
    assert win_a.status_code == 200, win_a.text
    assert win_a.json()["winning_contractors"] == 1
    assert win_a.json()["alert_queued"] is False

    # The second contractor's win should queue the alert.
    login_contractor(api_client, data["contractor_b"])

    win_b = api_client.patch(
        f"/jobs/{accept_b.json()['job_id']}/status",
        headers={"Origin": TEST_ORIGIN},
        json={"status": "BID_WON"},
    )
    assert win_b.status_code == 200, win_b.text
    assert win_b.json()["winning_contractors"] == 2
    assert win_b.json()["alert_queued"] is True

    # Retrying the same status should not queue another alert.
    retry_b = api_client.patch(
        f"/jobs/{accept_b.json()['job_id']}/status",
        headers={"Origin": TEST_ORIGIN},
        json={"status": "BID_WON"},
    )
    assert retry_b.status_code == 200, retry_b.text
    assert retry_b.json()["changed"] is False
    assert retry_b.json()["alert_queued"] is False

    # Verify that exactly one alert exists for this lead.
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT COUNT(*) AS event_count
                FROM outbox_events
                WHERE event_type = 'MultipleContractorsReportedBidWon'
                  AND payload->>'lead_id' = %s
                """,
                (data["lead_id"],),
            )
            event_count = cursor.fetchone()["event_count"]

    assert event_count == 1


def _accept_test_job(api_client, data):
    login_contractor(api_client, data["contractor_a"])

    response = api_client.post(
        f"/opportunities/{data['opportunity_id']}/respond",
        headers={"Origin": TEST_ORIGIN},
        json={"decision": "accept"},
    )
    assert response.status_code == 200, response.text
    return response.json()["job_id"]


def _set_test_job_in_progress(api_client, job_id):
    for status in ("BID_WON", "JOB_IN_PROGRESS"):
        response = api_client.patch(
            f"/jobs/{job_id}/status",
            headers={"Origin": TEST_ORIGIN},
            json={"status": status},
        )
        assert response.status_code == 200, response.text

def _approve_test_payment_agreement(api_client, data, job_id):
    response = api_client.post(
        f"/contractor/jobs/{job_id}/payment-agreement",
        headers={"Origin": TEST_ORIGIN},
        json={
            "arrangement_type": "PAID_IN_FULL",
            "invoice_total": "1000.00",
        },
    )
    assert response.status_code == 201, response.text

    accept_response = api_client.post(
        f"/contractor/jobs/{job_id}/payment-agreement/accept",
        headers={"Origin": TEST_ORIGIN},
    )
    assert accept_response.status_code == 200, accept_response.text

def test_contractor_can_delete_own_job_photo_while_in_progress(
    api_client,
    opportunity_owned_by_contractor,
):
    data = opportunity_owned_by_contractor
    job_id = _accept_test_job(api_client, data)
    _set_test_job_in_progress(api_client, job_id)

    upload_response = api_client.post(
        f"/contractor/jobs/{job_id}/photos",
        headers={"Origin": TEST_ORIGIN},
        files=[
            (
                "photos",
                ("deletable-photo.jpg", _make_test_jpeg(), "image/jpeg"),
            ),
        ],
    )
    assert upload_response.status_code == 201, upload_response.text

    photo_id = upload_response.json()[0]["photo_id"]

    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT s3_key
                FROM photos
                WHERE photo_id = %s
                """,
                (photo_id,),
            )
            photo_record = cursor.fetchone()

    assert photo_record is not None

    stored_path = (
        api.Path(__file__).resolve().parents[1]
        / "data"
        / "uploads"
        / photo_record["s3_key"].removeprefix("local/")
    )
    assert stored_path.is_file()

    try:
        delete_response = api_client.delete(
            f"/contractor/jobs/{job_id}/photos/{photo_id}",
            headers={"Origin": TEST_ORIGIN},
        )

        assert delete_response.status_code == 200, delete_response.text
        assert delete_response.json()["message"] == "Photo deleted successfully."
        assert not stored_path.exists()

        with get_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT COUNT(*) AS photo_count
                    FROM photos
                    WHERE photo_id = %s
                    """,
                    (photo_id,),
                )
                photo_count = cursor.fetchone()["photo_count"]

        assert photo_count == 0

    finally:
        stored_path.unlink(missing_ok=True)
        with get_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "DELETE FROM photos WHERE photo_id = %s",
                    (photo_id,),
                )

def test_contractor_cannot_delete_job_photo_after_permanent_lock(
    api_client,
    opportunity_owned_by_contractor,
):
    data = opportunity_owned_by_contractor
    job_id = _accept_test_job(api_client, data)
    _set_test_job_in_progress(api_client, job_id)

    upload_response = api_client.post(
        f"/contractor/jobs/{job_id}/photos",
        headers={"Origin": TEST_ORIGIN},
        files=[
            (
                "photos",
                ("locked-photo.jpg", _make_test_jpeg(), "image/jpeg"),
            ),
        ],
    )
    assert upload_response.status_code == 201, upload_response.text

    photo_id = upload_response.json()[0]["photo_id"]

    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT s3_key
                FROM photos
                WHERE photo_id = %s
                """,
                (photo_id,),
            )
            photo_record = cursor.fetchone()

            cursor.execute(
                """
                UPDATE jobs
                SET photos_locked_at = CURRENT_TIMESTAMP
                WHERE job_id = %s
                """,
                (job_id,),
            )

    assert photo_record is not None

    stored_path = (
        api.Path(__file__).resolve().parents[1]
        / "data"
        / "uploads"
        / photo_record["s3_key"].removeprefix("local/")
    )

    try:
        assert stored_path.is_file()

        delete_response = api_client.delete(
            f"/contractor/jobs/{job_id}/photos/{photo_id}",
            headers={"Origin": TEST_ORIGIN},
        )

        assert delete_response.status_code == 409, delete_response.text

        assert stored_path.is_file()

        with get_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT COUNT(*) AS photo_count
                    FROM photos
                    WHERE photo_id = %s
                    """,
                    (photo_id,),
                )
                photo_count = cursor.fetchone()["photo_count"]

        assert photo_count == 1

    finally:
        stored_path.unlink(missing_ok=True)
        with get_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "DELETE FROM photos WHERE photo_id = %s",
                    (photo_id,),
                )

def test_contractor_cannot_delete_homeowner_job_photo(
    api_client,
    opportunity_owned_by_contractor,
):
    data = opportunity_owned_by_contractor
    job_id = _accept_test_job(api_client, data)
    _set_test_job_in_progress(api_client, job_id)

    photo_id = f"EMGP-TEST-{secrets.token_hex(6).upper()}"
    stored_name = f"{photo_id}.jpg"

    upload_dir = (
        api.Path(__file__).resolve().parents[1]
        / "data"
        / "uploads"
    )
    upload_dir.mkdir(parents=True, exist_ok=True)
    stored_path = upload_dir / stored_name

    stored_path.write_bytes(_make_test_jpeg())

    try:
        with get_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT lead_id
                    FROM jobs
                    WHERE job_id = %s
                    """,
                    (job_id,),
                )
                job = cursor.fetchone()

                assert job is not None

                cursor.execute(
                    """
                    INSERT INTO photos (
                        photo_id,
                        lead_id,
                        s3_key,
                        original_filename,
                        content_type
                    )
                    VALUES (%s, %s, %s, %s, %s)
                    """,
                    (
                        photo_id,
                        job["lead_id"],
                        f"local/{stored_name}",
                        "homeowner-photo.jpg",
                        "image/jpeg",
                    ),
                )

        delete_response = api_client.delete(
            f"/contractor/jobs/{job_id}/photos/{photo_id}",
            headers={"Origin": TEST_ORIGIN},
        )

        assert delete_response.status_code == 404, delete_response.text
        assert stored_path.is_file()

        with get_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT COUNT(*) AS photo_count
                    FROM photos
                    WHERE photo_id = %s
                    """,
                    (photo_id,),
                )
                photo_count = cursor.fetchone()["photo_count"]

        assert photo_count == 1

    finally:
        stored_path.unlink(missing_ok=True)
        with get_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "DELETE FROM photos WHERE photo_id = %s",
                    (photo_id,),
                )

def test_contractor_cannot_upload_job_photos_after_permanent_lock(
    api_client,
    opportunity_owned_by_contractor,
):
    data = opportunity_owned_by_contractor
    job_id = _accept_test_job(api_client, data)
    _set_test_job_in_progress(api_client, job_id)

    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE jobs
                SET photos_locked_at = CURRENT_TIMESTAMP
                WHERE job_id = %s
                """,
                (job_id,),
            )

    upload_response = api_client.post(
        f"/contractor/jobs/{job_id}/photos",
        headers={"Origin": TEST_ORIGIN},
        files=[
            (
                "photos",
                ("locked-photo.jpg", _make_test_jpeg(), "image/jpeg"),
            ),
        ],
    )

    assert upload_response.status_code == 409, upload_response.text

def test_job_photo_lock_is_set_on_completion_and_never_cleared(
    api_client,
    opportunity_owned_by_contractor,
):
    data = opportunity_owned_by_contractor
    job_id = _accept_test_job(api_client, data)
    _set_test_job_in_progress(api_client, job_id)
    agreement_response = api_client.post(
        f"/contractor/jobs/{job_id}/payment-agreement",
        headers={"Origin": TEST_ORIGIN},
        json={
            "arrangement_type": "PAYMENT_PLAN",
            "invoice_total": "1000.00",
            "down_payment_amount": "0.00",
            "down_payment_due_date": "2026-11-12",
            "installment_count": 2,
            "schedule_type": "MONTHLY",
            "term_months": 1,
            "installment_amount": "500.00",
            "final_installment_amount": "500.00",
            "plan_start_date": "2026-11-15",
            "final_due_date": "2026-12-15",
        },
    )
    assert agreement_response.status_code == 201, agreement_response.text

    accept_response = api_client.post(
        f"/contractor/jobs/{job_id}/payment-agreement/accept",
        headers={"Origin": TEST_ORIGIN},
    )
    assert accept_response.status_code == 200, accept_response.text

    # Completing the job should create the permanent photo lock.
    response = api_client.patch(
        f"/jobs/{job_id}/status",
        headers={"Origin": TEST_ORIGIN},
        json={"status": "JOB_COMPLETED"},
    )
    assert response.status_code == 200, response.text
    # A repeated completion request must not create another invitation.
    retry_response = api_client.patch(
        f"/jobs/{job_id}/status",
        headers={"Origin": TEST_ORIGIN},
        json={"status": "JOB_COMPLETED"},
    )
    assert retry_response.status_code == 200, retry_response.text

    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT status, photos_locked_at
                FROM jobs
                WHERE job_id = %s
                """,
                (job_id,),
            )
            job = cursor.fetchone()
            cursor.execute(
                """
                SELECT
                    i.delivery_status,
                    i.retry_state,
                    i.customer_email,
                    i.token_hash,
                    i.created_at,
                    i.expires_at,
                    i.next_attempt_at,
                    l.customer_email AS lead_email,
                    COUNT(*) OVER () AS invitation_count
                FROM homeowner_review_invitations i
                JOIN jobs j ON j.id = i.job_id
                JOIN leads l ON l.id = j.lead_id
                WHERE j.job_id = %s
                """,
                (job_id,),
            )
            invitation = cursor.fetchone()

    assert job is not None
    assert job["status"] == "JOB_COMPLETED"
    assert job["photos_locked_at"] is not None
    assert invitation is not None
    assert invitation["invitation_count"] == 1
    assert invitation["customer_email"] == invitation["lead_email"]
    assert invitation["delivery_status"] == "PENDING"
    assert invitation["retry_state"] == "ACTIVE"
    assert invitation["next_attempt_at"] is not None
    assert invitation["expires_at"] - invitation["created_at"] == (
        timedelta(days=30)
    )
    assert invitation["token_hash"] is not None
    assert len(invitation["token_hash"].strip()) == 64

    # Save the original timestamp to verify it is never overwritten.
    locked_at = job["photos_locked_at"]

    # Payment received is the only permitted transition after completion.
    response = api_client.patch(
        f"/jobs/{job_id}/status",
        headers={"Origin": TEST_ORIGIN},
        json={"status": "PAYMENT_RECEIVED"},
    )
    assert response.status_code == 200, response.text

    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT status, photos_locked_at
                FROM jobs
                WHERE job_id = %s
                """,
                (job_id,),
            )
            job = cursor.fetchone()

    assert job is not None
    assert job["status"] == "PAYMENT_RECEIVED"
    assert job["photos_locked_at"] == locked_at

def test_homeowner_review_invitation_sent_through_ses(
    api_client,
    opportunity_owned_by_contractor,
    monkeypatch,
):
    data = opportunity_owned_by_contractor
    job_id = _accept_test_job(api_client, data)
    _set_test_job_in_progress(api_client, job_id)
    _approve_test_payment_agreement(api_client, data, job_id)

    response = api_client.patch(
        f"/jobs/{job_id}/status",
        headers={"Origin": TEST_ORIGIN},
        json={"status": "JOB_COMPLETED"},
    )
    assert response.status_code == 200, response.text

    monkeypatch.setenv(
        "EMG_REVIEW_TOKEN_SECRET",
        "test-review-token-secret-for-automated-tests",
    )
    monkeypatch.setenv(
        "EMG_REVIEW_EMAIL_FROM",
        "reviews@example.com",
    )
    monkeypatch.setenv(
        "EMG_REVIEW_BASE_URL",
        "https://elitematchgroup.com/review",
    )
    monkeypatch.setenv("EMG_SES_REGION", "us-west-2")

    captured = {}

    class FakeSES:
        def send_email(self, **kwargs):
            captured.update(kwargs)
            return {"MessageId": "fake-ses-message-123"}

    def fake_boto_client(service, **kwargs):
        assert service == "ses"
        assert kwargs["region_name"] == "us-west-2"
        return FakeSES()

    monkeypatch.setattr(
        homeowner_review_delivery.boto3,
        "client",
        fake_boto_client,
    )
    deferred_invitations = []

    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT id, next_attempt_at
                FROM homeowner_review_invitations
                WHERE id <> (
                    SELECT i.id
                    FROM homeowner_review_invitations i
                    JOIN jobs j ON j.id = i.job_id
                    WHERE j.job_id = %s
                )
                  AND retry_state = 'ACTIVE'
                  AND delivery_status IN ('PENDING', 'FAILED')
                  AND submitted_at IS NULL
                  AND claimed_until IS NULL
                """,
                (job_id,),
            )
            deferred_invitations = cursor.fetchall()

            cursor.execute(
                """
                UPDATE homeowner_review_invitations
                SET next_attempt_at = CURRENT_TIMESTAMP + INTERVAL '1 day'
                WHERE id <> (
                    SELECT i.id
                    FROM homeowner_review_invitations i
                    JOIN jobs j ON j.id = i.job_id
                    WHERE j.job_id = %s
                )
                  AND retry_state = 'ACTIVE'
                  AND delivery_status IN ('PENDING', 'FAILED')
                  AND submitted_at IS NULL
                  AND claimed_until IS NULL
                """,
                (job_id,),
            )

    try:
        result = (
            homeowner_review_delivery.deliver_homeowner_review_invitations(
                batch_size=1
            )
        )
    finally:
        with get_connection() as connection:
            with connection.cursor() as cursor:
                cursor.executemany(
                    """
                    UPDATE homeowner_review_invitations
                    SET next_attempt_at = %s
                    WHERE id = %s
                    """,
                    [
                        (row["next_attempt_at"], row["id"])
                        for row in deferred_invitations
                    ],
                )

    assert result["claimed_count"] == 1
    assert result["sent_count"] == 1
    assert result["failed_count"] == 0
    assert result["paused_invitation_ids"] == []
    assert result["claim_lost_invitation_ids"] == []

    assert captured["Source"] == "reviews@example.com"
    assert len(captured["Destination"]["ToAddresses"]) == 1
    assert captured["Destination"]["ToAddresses"][0].endswith(
        "@example.com"
    )

    email_text = captured["Message"]["Body"]["Text"]["Data"]
    review_url = next(
        line
        for line in email_text.splitlines()
        if line.startswith("https://")
    )
    token = parse_qs(urlparse(review_url).query)["token"][0]

    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT
                    i.id,
                    i.delivery_status,
                    i.retry_state,
                    i.attempts,
                    i.token_hash,
                    i.email_sent_at,
                    i.provider_message_id,
                    i.claimed_until,
                    i.next_attempt_at
                FROM homeowner_review_invitations i
                JOIN jobs j ON j.id = i.job_id
                WHERE j.job_id = %s
                """,
                (job_id,),
            )
            invitation = cursor.fetchone()

            cursor.execute(
                """
                SELECT event_type
                FROM homeowner_review_delivery_events
                WHERE invitation_id = (
                    SELECT i.id
                    FROM homeowner_review_invitations i
                    JOIN jobs j ON j.id = i.job_id
                    WHERE j.job_id = %s
                )
                ORDER BY id
                """,
                (job_id,),
            )
            events = [
                row["event_type"]
                for row in cursor.fetchall()
            ]

    assert invitation is not None
    assert invitation["delivery_status"] == "SENT", (
        f"result={result!r}; invitation={invitation!r}"
    )
    assert invitation["retry_state"] == "COMPLETE"
    assert invitation["attempts"] == 1
    assert invitation["email_sent_at"] is not None
    assert invitation["provider_message_id"] == "fake-ses-message-123"
    assert invitation["claimed_until"] is None
    assert invitation["next_attempt_at"] is None
    assert review_token_matches(token, invitation["token_hash"])

    assert events == [
        "ATTEMPT_STARTED",
        "ATTEMPT_SUCCEEDED",
        "COMPLETED",
    ]


def test_homeowner_review_invitation_pauses_after_five_failures(
    api_client,
    opportunity_owned_by_contractor,
    monkeypatch,
):
    data = opportunity_owned_by_contractor
    job_id = _accept_test_job(api_client, data)
    _set_test_job_in_progress(api_client, job_id)
    _approve_test_payment_agreement(api_client, data, job_id)

    response = api_client.patch(
        f"/jobs/{job_id}/status",
        headers={"Origin": TEST_ORIGIN},
        json={"status": "JOB_COMPLETED"},
    )
    assert response.status_code == 200, response.text

    monkeypatch.setenv(
        "EMG_REVIEW_TOKEN_SECRET",
        "test-review-token-secret-for-automated-tests",
    )
    monkeypatch.setenv(
        "EMG_REVIEW_EMAIL_FROM",
        "reviews@example.com",
    )
    monkeypatch.setenv(
        "EMG_REVIEW_BASE_URL",
        "https://elitematchgroup.com/review",
    )
    monkeypatch.setenv("EMG_SES_REGION", "us-west-2")

    class FailingSES:
        def send_email(self, **kwargs):
            raise ClientError(
                {
                    "Error": {
                        "Code": "MessageRejected",
                        "Message": "Simulated SES delivery failure",
                    },
                    "ResponseMetadata": {
                        "HTTPStatusCode": 400,
                    },
                },
                "SendEmail",
            )

    def fake_boto_client(service, **kwargs):
        assert service == "ses"
        return FailingSES()

    monkeypatch.setattr(
        homeowner_review_delivery.boto3,
        "client",
        fake_boto_client,
    )

    for attempt in range(1, 6):
        result = (
            homeowner_review_delivery.deliver_homeowner_review_invitations(
                batch_size=1
            )
        )

        assert result["claimed_count"] == 1
        assert result["sent_count"] == 0
        assert result["failed_count"] == 1
        assert result["failed_invitations"][0]["failure_streak"] == attempt

        if attempt < 5:
            assert result["paused_invitation_ids"] == []

            # Make the next retry immediately eligible.
            with get_connection() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        UPDATE homeowner_review_invitations i
                        SET next_attempt_at = CURRENT_TIMESTAMP
                        FROM jobs j
                        WHERE i.job_id = j.id
                          AND j.job_id = %s
                        """,
                        (job_id,),
                    )
        else:
            assert result["paused_invitation_ids"] == [
                result["failed_invitations"][0]["invitation_id"]
            ]

    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT
                    i.delivery_status,
                    i.retry_state,
                    i.attempts,
                    i.failure_streak,
                    i.paused_at,
                    i.next_attempt_at,
                    i.claimed_until,
                    i.last_error
                FROM homeowner_review_invitations i
                JOIN jobs j ON j.id = i.job_id
                WHERE j.job_id = %s
                """,
                (job_id,),
            )
            invitation = cursor.fetchone()

            cursor.execute(
                """
                SELECT event_type, COUNT(*) AS event_count
                FROM homeowner_review_delivery_events
                WHERE invitation_id = (
                    SELECT i.id
                    FROM homeowner_review_invitations i
                    JOIN jobs j ON j.id = i.job_id
                    WHERE j.job_id = %s
                )
                GROUP BY event_type
                """,
                (job_id,),
            )
            events = {
                row["event_type"]: row["event_count"]
                for row in cursor.fetchall()
            }

    assert invitation is not None
    assert invitation["delivery_status"] == "FAILED"
    assert invitation["retry_state"] == "PAUSED"
    assert invitation["attempts"] == 5
    assert invitation["failure_streak"] == 5
    assert invitation["paused_at"] is not None
    assert invitation["next_attempt_at"] is None
    assert invitation["claimed_until"] is None
    assert "Simulated SES delivery failure" in invitation["last_error"]

    assert events == {
        "ATTEMPT_STARTED": 5,
        "ATTEMPT_FAILED": 5,
        "PAUSED": 1,
    }

def test_homeowner_review_invitation_can_be_resumed_and_stopped(
    api_client,
    opportunity_owned_by_contractor,
):
    data = opportunity_owned_by_contractor
    job_id = _accept_test_job(api_client, data)
    _set_test_job_in_progress(api_client, job_id)
    _approve_test_payment_agreement(api_client, data, job_id)

    response = api_client.patch(
        f"/jobs/{job_id}/status",
        headers={"Origin": TEST_ORIGIN},
        json={"status": "JOB_COMPLETED"},
    )
    assert response.status_code == 200, response.text

    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT i.id
                FROM homeowner_review_invitations i
                JOIN jobs j ON j.id = i.job_id
                WHERE j.job_id = %s
                """,
                (job_id,),
            )
            invitation_id = cursor.fetchone()["id"]

            cursor.execute(
                """
                UPDATE homeowner_review_invitations
                SET retry_state = 'PAUSED',
                    delivery_status = 'FAILED',
                    attempts = 5,
                    failure_streak = 5,
                    paused_at = CURRENT_TIMESTAMP,
                    next_attempt_at = NULL,
                    claimed_until = CURRENT_TIMESTAMP + INTERVAL '1 minute'
                WHERE id = %s
                """,
                (invitation_id,),
            )

    result = homeowner_review_delivery.resume_homeowner_review_invitation(
        invitation_id=invitation_id,
        admin_id=None,
        reason="  Reviewed SES configuration  ",
    )

    assert result["invitation_id"] == invitation_id
    assert result["retry_state"] == "ACTIVE"

    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT
                    retry_state,
                    failure_streak,
                    paused_at,
                    next_attempt_at,
                    claimed_until,
                    retry_control_admin_id,
                    retry_control_reason
                FROM homeowner_review_invitations
                WHERE id = %s
                """,
                (invitation_id,),
            )
            invitation = cursor.fetchone()

    assert invitation["retry_state"] == "ACTIVE"
    assert invitation["failure_streak"] == 0
    assert invitation["paused_at"] is None
    assert invitation["next_attempt_at"] is not None
    assert invitation["claimed_until"] is None
    assert invitation["retry_control_admin_id"] is None
    assert invitation["retry_control_reason"] == "Reviewed SES configuration"

    result = homeowner_review_delivery.stop_homeowner_review_invitation(
        invitation_id=invitation_id,
        admin_id=None,
        reason="Administrator stopped automatic retries",
    )

    assert result["invitation_id"] == invitation_id
    assert result["retry_state"] == "STOPPED"

    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT
                    retry_state,
                    stopped_at,
                    paused_at,
                    next_attempt_at,
                    claimed_until,
                    retry_control_reason
                FROM homeowner_review_invitations
                WHERE id = %s
                """,
                (invitation_id,),
            )
            invitation = cursor.fetchone()

            cursor.execute(
                """
                SELECT event_type, admin_user_id, details
                FROM homeowner_review_delivery_events
                WHERE invitation_id = %s
                  AND event_type IN ('RESUMED', 'STOPPED')
                ORDER BY id
                """,
                (invitation_id,),
            )
            events = cursor.fetchall()

    assert invitation["retry_state"] == "STOPPED"
    assert invitation["stopped_at"] is not None
    assert invitation["paused_at"] is None
    assert invitation["next_attempt_at"] is None
    assert invitation["claimed_until"] is None
    assert invitation["retry_control_reason"] == (
        "Administrator stopped automatic retries"
    )

    assert [event["event_type"] for event in events] == [
        "RESUMED",
        "STOPPED",
    ]
    assert all(event["admin_user_id"] is None for event in events)
    assert events[0]["details"]["reason"] == "Reviewed SES configuration"
    assert events[1]["details"]["reason"] == (
        "Administrator stopped automatic retries"
    )

    with pytest.raises(
        homeowner_review_delivery.ReviewInvitationConflictError,
        match="Only paused invitations can be resumed",
    ):
        homeowner_review_delivery.resume_homeowner_review_invitation(
            invitation_id=invitation_id,
            admin_id=None,
            reason="Attempt to resume stopped invitation",
        )


def test_homeowner_review_invitation_expiration_preserves_submitted_and_delivery_state(
    api_client,
    opportunity_owned_by_contractor,
):
    data = opportunity_owned_by_contractor
    job_id = _accept_test_job(api_client, data)
    _set_test_job_in_progress(api_client, job_id)
    _approve_test_payment_agreement(api_client, data, job_id)

    response = api_client.patch(
        f"/jobs/{job_id}/status",
        headers={"Origin": TEST_ORIGIN},
        json={"status": "JOB_COMPLETED"},
    )
    assert response.status_code == 200, response.text

    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT i.id
                FROM homeowner_review_invitations i
                JOIN jobs j ON j.id = i.job_id
                WHERE j.job_id = %s
                """,
                (job_id,),
            )
            invitation_id = cursor.fetchone()["id"]

            cursor.execute(
                """
                UPDATE homeowner_review_invitations
                SET created_at = CURRENT_TIMESTAMP - INTERVAL '31 days',
                    expires_at = CURRENT_TIMESTAMP - INTERVAL '1 day',
                    submitted_at = CURRENT_TIMESTAMP,
                    retry_state = 'COMPLETE',
                    delivery_status = 'SENT',
                    email_sent_at = CURRENT_TIMESTAMP,
                    next_attempt_at = NULL,
                    claimed_until = NULL
                WHERE id = %s
                """,
                (invitation_id,),
            )

    # Submitted invitations must not expire.
    result = homeowner_review_delivery.expire_homeowner_review_invitations()

    assert result["expired_count"] == 0
    assert result["expired_invitation_ids"] == []

    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT retry_state, delivery_status, submitted_at
                FROM homeowner_review_invitations
                WHERE id = %s
                """,
                (invitation_id,),
            )
            invitation = cursor.fetchone()

    assert invitation["retry_state"] == "COMPLETE"
    assert invitation["delivery_status"] == "SENT"
    assert invitation["submitted_at"] is not None

    # Once unsubmitted, the same overdue invitation should expire.
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE homeowner_review_invitations
                SET submitted_at = NULL
                WHERE id = %s
                """,
                (invitation_id,),
            )

    result = homeowner_review_delivery.expire_homeowner_review_invitations()

    assert result["expired_count"] == 1
    assert result["expired_invitation_ids"] == [invitation_id]

    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT
                    retry_state,
                    delivery_status,
                    email_sent_at,
                    next_attempt_at,
                    claimed_until,
                    paused_at,
                    stopped_at
                FROM homeowner_review_invitations
                WHERE id = %s
                """,
                (invitation_id,),
            )
            invitation = cursor.fetchone()

            cursor.execute(
                """
                SELECT event_type, attempt_number, failure_streak, details
                FROM homeowner_review_delivery_events
                WHERE invitation_id = %s
                  AND event_type = 'EXPIRED'
                """,
                (invitation_id,),
            )
            expiration_event = cursor.fetchone()

    assert invitation["retry_state"] == "EXPIRED"
    assert invitation["delivery_status"] == "SENT"
    assert invitation["email_sent_at"] is not None
    assert invitation["next_attempt_at"] is None
    assert invitation["claimed_until"] is None
    assert invitation["paused_at"] is None
    assert invitation["stopped_at"] is None

    assert expiration_event is not None
    assert expiration_event["attempt_number"] is None
    assert expiration_event["failure_streak"] == 0
    assert expiration_event["details"]["reason"] == (
        "The 30-day review invitation deadline passed"
    )


def test_homeowner_review_invitation_admin_actions_validate_inputs():
    with pytest.raises(ValueError, match="at least 3 characters"):
        homeowner_review_delivery.resume_homeowner_review_invitation(
            invitation_id=999999,
            admin_id=None,
            reason="  ",
        )

    with pytest.raises(ValueError, match="at least 3 characters"):
        homeowner_review_delivery.stop_homeowner_review_invitation(
            invitation_id=999999,
            admin_id=None,
            reason=" x ",
        )

    with pytest.raises(
        homeowner_review_delivery.ReviewInvitationNotFoundError,
        match="Review invitation not found",
    ):
        homeowner_review_delivery.resume_homeowner_review_invitation(
            invitation_id=999999,
            admin_id=None,
            reason="Valid test reason",
        )

    with pytest.raises(
        homeowner_review_delivery.ReviewInvitationNotFoundError,
        match="Review invitation not found",
    ):
        homeowner_review_delivery.stop_homeowner_review_invitation(
            invitation_id=999999,
            admin_id=None,
            reason="Valid test reason",
        )

    with pytest.raises(ValueError, match="Batch size"):
        homeowner_review_delivery.expire_homeowner_review_invitations(
            batch_size=0
        )

    with pytest.raises(ValueError, match="Batch size"):
        homeowner_review_delivery.expire_homeowner_review_invitations(
            batch_size=101
        )

def test_contractor_job_photo_upload_rejected_before_job_in_progress(
    api_client,
    opportunity_owned_by_contractor,
):
    data = opportunity_owned_by_contractor
    job_id = _accept_test_job(api_client, data)

    response = api_client.post(
        f"/contractor/jobs/{job_id}/photos",
        headers={"Origin": TEST_ORIGIN},
        files=[
            ("photos", ("test.jpg", b"\xff\xd8\xfftest", "image/jpeg")),
        ],
    )

    assert response.status_code == 409, response.text
    assert "in progress" in response.json()["detail"].lower()


def test_contractor_job_photo_upload_requires_authentication(
    api_client,
    opportunity_owned_by_contractor,
):
    data = opportunity_owned_by_contractor
    job_id = _accept_test_job(api_client, data)
    _set_test_job_in_progress(api_client, job_id)

    api_client.cookies.clear()

    response = api_client.post(
        f"/contractor/jobs/{job_id}/photos",
        headers={"Origin": TEST_ORIGIN},
        files=[
            ("photos", ("test.jpg", b"\xff\xd8\xfftest", "image/jpeg")),
        ],
    )

    assert response.status_code == 401, response.text


def test_contractor_cannot_upload_photos_to_another_contractors_job(
    api_client,
    opportunity_owned_by_contractor,
):
    data = opportunity_owned_by_contractor
    job_id = _accept_test_job(api_client, data)
    _set_test_job_in_progress(api_client, job_id)

    api_client.cookies.clear()
    login_contractor(api_client, data["contractor_b"])

    response = api_client.post(
        f"/contractor/jobs/{job_id}/photos",
        headers={"Origin": TEST_ORIGIN},
        files=[
            ("photos", ("test.jpg", b"\xff\xd8\xfftest", "image/jpeg")),
        ],
    )

    assert response.status_code == 404, response.text


def test_contractor_job_photo_upload_rejects_invalid_image(
    api_client,
    opportunity_owned_by_contractor,
):
    data = opportunity_owned_by_contractor
    job_id = _accept_test_job(api_client, data)
    _set_test_job_in_progress(api_client, job_id)

    response = api_client.post(
        f"/contractor/jobs/{job_id}/photos",
        headers={"Origin": TEST_ORIGIN},
        files=[
            ("photos", ("test.jpg", b"not an image", "image/jpeg")),
        ],
    )

    assert response.status_code == 422, response.text
    assert "valid JPEG, PNG, or WebP" in response.json()["detail"]

def _make_test_jpeg():
    image = Image.new("RGB", (10, 10), color="white")
    image_bytes = io.BytesIO()
    image.save(image_bytes, format="JPEG")
    return image_bytes.getvalue()

def test_contractor_job_photo_upload_enforces_fifteen_photo_total(
    api_client,
    opportunity_owned_by_contractor,
):
    data = opportunity_owned_by_contractor
    job_id = _accept_test_job(api_client, data)
    _set_test_job_in_progress(api_client, job_id)

    upload_url = f"/contractor/jobs/{job_id}/photos"

    try:
        first_upload = api_client.post(
            upload_url,
            headers={"Origin": TEST_ORIGIN},
            files=[
                (
                    "photos",
                    (
                        f"contractor-photo-{index}.jpg",
                        _make_test_jpeg(),
                        "image/jpeg",
                    ),
                )
                for index in range(10)
            ],
        )

        assert first_upload.status_code == 201, first_upload.text
        assert len(first_upload.json()) == 10

        second_upload = api_client.post(
            upload_url,
            headers={"Origin": TEST_ORIGIN},
            files=[
                (
                    "photos",
                    (
                        f"additional-photo-{index}.jpg",
                        _make_test_jpeg(),
                        "image/jpeg",
                    ),
                )
                for index in range(6)
            ],
        )

        assert second_upload.status_code == 422, second_upload.text
        assert "fifteen photos" in second_upload.json()["detail"]

        with get_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT COUNT(*) AS photo_count
                    FROM photos
                    WHERE job_id = (
                        SELECT id FROM jobs WHERE job_id = %s
                    )
                    """,
                    (job_id,),
                )
                photo_count = cursor.fetchone()["photo_count"]

        assert photo_count == 10

    finally:
        with get_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT s3_key
                    FROM photos
                    WHERE job_id = (
                        SELECT id FROM jobs WHERE job_id = %s
                    )
                    """,
                    (job_id,),
                )
                photo_records = cursor.fetchall()

        upload_dir = (
            api.Path(__file__).resolve().parents[1]
            / "data"
            / "uploads"
        )

        for record in photo_records:
            stored_name = record["s3_key"].removeprefix("local/")
            (upload_dir / stored_name).unlink(missing_ok=True)

def test_contractor_mpo_photo_upload_is_normalized_to_jpeg(
    api_client,
    opportunity_owned_by_contractor,
    monkeypatch,
):
    data = opportunity_owned_by_contractor
    job_id = _accept_test_job(api_client, data)
    _set_test_job_in_progress(api_client, job_id)

    # Preserve the real image module's open function before mocking it.
    real_image_open = api.Image.open

    class FakeMpoImage:
        format = "MPO"

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc_value, traceback):
            return False

        def verify(self):
            pass

        def seek(self, frame):
            assert frame == 0

        def convert(self, mode):
            assert mode == "RGB"
            return Image.new("RGB", (20, 20), color="white")

    monkeypatch.setattr(
        api.Image,
        "open",
        lambda *args, **kwargs: FakeMpoImage(),
    )

    stored_path = None

    try:
        response = api_client.post(
            f"/contractor/jobs/{job_id}/photos",
            headers={"Origin": TEST_ORIGIN},
            files=[
                (
                    "photos",
                    ("contractor-progress.jpeg", b"simulated MPO data", "image/jpeg"),
                ),
            ],
        )

        assert response.status_code == 201, response.text
        uploaded = response.json()
        assert len(uploaded) == 1

        photo_id = uploaded[0]["photo_id"]

        with get_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT s3_key, content_type
                    FROM photos
                    WHERE photo_id = %s
                    """,
                    (photo_id,),
                )
                photo_record = cursor.fetchone()

        assert photo_record is not None
        assert photo_record["content_type"] == "image/jpeg"

        stored_path = (
            api.Path(__file__).resolve().parents[1]
            / "data"
            / "uploads"
            / photo_record["s3_key"].removeprefix("local/")
        )

        # Verify the actual stored bytes are a decodable JPEG.
        with real_image_open(stored_path) as stored_image:
            assert stored_image.format == "JPEG"
            stored_image.load()

    finally:
        if stored_path is not None:
            stored_path.unlink(missing_ok=True)

def test_uploaded_contractor_photo_appears_in_admin_job_photos(
    api_client,
    opportunity_owned_by_contractor,
    monkeypatch,
):
    data = opportunity_owned_by_contractor
    job_id = _accept_test_job(api_client, data)
    _set_test_job_in_progress(api_client, job_id)

    # This test verifies the photo query and response; admin authentication
    # itself is covered elsewhere.
    monkeypatch.setattr(api, "require_admin", lambda request: None)

    response = api_client.post(
        f"/contractor/jobs/{job_id}/photos",
        headers={"Origin": TEST_ORIGIN},
        files=[
            (
                "photos",
                (
                    "contractor-progress.jpg",
                    _make_test_jpeg(),
                    "image/jpeg",
                ),
            ),
        ],
    )

    assert response.status_code == 201, response.text
    uploaded = response.json()
    assert len(uploaded) == 1
    photo_id = uploaded[0]["photo_id"]
    assert uploaded[0]["filename"] == "contractor-progress.jpg"

    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT s3_key
                FROM photos
                WHERE photo_id = %s
                """,
                (photo_id,),
            )
            photo_record = cursor.fetchone()

    assert photo_record is not None

    stored_path = (
        api.Path(__file__).resolve().parents[1]
        / "data"
        / "uploads"
        / photo_record["s3_key"].removeprefix("local/")
    )

    try:
        api_client.cookies.clear()
        admin_response = api_client.get(
            f"/admin/jobs/{job_id}/photos",
        )

        assert admin_response.status_code == 200, admin_response.text
        admin_photos = admin_response.json()
        matching = [
            photo for photo in admin_photos
            if photo["photo_id"] == photo_id
        ]
        assert len(matching) == 1
        assert matching[0]["url"] == f"/admin/photos/{photo_id}"
    finally:
        stored_path.unlink(missing_ok=True)