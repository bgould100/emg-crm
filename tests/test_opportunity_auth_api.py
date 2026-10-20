
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
            "lead_id": lead_id,
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

                if lead_id:
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
    assert detail["service_type"] == "Roofing"
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
