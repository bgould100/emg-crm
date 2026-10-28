
import http.cookiejar
import json
import os
import urllib.error
import urllib.request
import uuid

import pytest

from dotenv import load_dotenv

from app.database.connection import get_connection
from app.database.contractors import create_contractor
from app.database.leads import create_lead
from app.services.matching import route_opportunities_to_fill_slots
from app.services.opportunities import respond_to_opportunity
from app.services.job_status import update_job_status


def _contractor_pk(opportunity_id):
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT contractor_id FROM opportunities WHERE opportunity_id = %s",
                (opportunity_id,),
            )
            row = cursor.fetchone()
    assert row is not None
    return row["contractor_id"]


def _request_json(opener, url, method="GET", payload=None, origin=None):
    data = None
    headers = {}

    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"

    if origin:
        headers["Origin"] = origin

    request = urllib.request.Request(
        url,
        data=data,
        headers=headers,
        method=method,
    )

    try:
        with opener.open(request, timeout=15) as response:
            body = response.read().decode("utf-8")
            return response.status, json.loads(body) if body else {}
    except urllib.error.HTTPError as error:
        body = error.read().decode("utf-8", errors="replace")
        raise AssertionError(
            f"{method} {url} returned HTTP {error.code}: {body}"
        ) from error


def test_http_admin_release_slot_creates_replacement():
    load_dotenv()

    username = os.getenv("EMG_ADMIN_USERNAME")
    password = os.getenv("EMG_ADMIN_PASSWORD")
    base_url = os.getenv(
        "EMG_TEST_BASE_URL", "http://localhost:8000"
    ).rstrip("/")
    origin = os.getenv("EMG_ALLOWED_ORIGIN", "http://localhost:8000").rstrip("/")

    if not username or not password:
        pytest.skip(
            "Admin HTTP integration test requires "
            "EMG_ADMIN_USERNAME and EMG_ADMIN_PASSWORD."
        )

    # Retain the administrator's session cookie across HTTP requests.
    cookie_jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(cookie_jar)
    )

    # Authenticate against the real API.
    login_status, login_body = _request_json(
        opener,
        f"{base_url}/admin-auth/login",
        method="POST",
        payload={"username": username, "password": password},
        origin=origin,
    )

    assert login_status == 200, login_body
    assert login_body.get("authenticated") is True, login_body
    assert any(
        cookie.name == "emg_admin_session" for cookie in cookie_jar
    ), "Login succeeded but the administrator session cookie was not set."

    # Confirm the cookie authenticates the session.
    session_status, session_body = _request_json(
        opener,
        f"{base_url}/admin-auth/me",
    )
    assert session_status == 200, session_body
    assert session_body.get("authenticated") is True, session_body

    suffix = uuid.uuid4().hex[:10]
    lead_id = None
    contractor_ids = []

    try:
        lead = create_lead(
            zip_code="83814",
            service_type="Roofing",
            description=f"HTTP slot-release test {suffix}",
            requested_bid_count=1,
            customer_name=f"HTTP Test {suffix}",
            customer_email=f"http-slot-{suffix}@example.com",
        )
        lead_id = lead["lead_id"]

        for number in range(3):
            contractor = create_contractor(
                company_name=f"EMG HTTP Slot Test {suffix} {number}",
                email=f"http-slot-{suffix}-{number}@example.com",
                service_area=["83814"],
                specialties=["Roofing"],
            )
            contractor_ids.append(contractor["contractor_id"])

        initial = route_opportunities_to_fill_slots(lead_id)
        assert len(initial) == 1

        original_opportunity_id = initial[0]["opportunity_id"]
        original_contractor_pk = _contractor_pk(original_opportunity_id)

        accepted = respond_to_opportunity(
            original_opportunity_id, "accept", original_contractor_pk
        )
        assert accepted["status"] == "ACCEPTED"

        job_update = update_job_status(
            accepted["job_id"], "BID_LOST", original_contractor_pk
        )
        assert job_update["status"] == "BID_LOST"

        # Release the accepted slot using the authenticated session cookie.
        status_code, response_body = _request_json(
            opener,
            (
                f"{base_url}/admin/opportunities/"
                f"{original_opportunity_id}/release-slot"
            ),
            method="POST",
            payload={"reason": "HTTP integration test release"},
            origin=origin,
        )

        assert status_code == 200, response_body

        # Retry routing through HTTP. The replacement is already pending,
        # so this must not create a duplicate invitation.
        retry_status_code, retry_body = _request_json(
            opener,
            (
                f"{base_url}/admin/opportunities/"
                f"{original_opportunity_id}/retry-slot-routing"
            ),
            method="POST",
            payload={"reason": "HTTP integration test routing retry"},
            origin=origin,
        )

        assert retry_status_code == 200, retry_body
        assert retry_body["action"] == "RETRY_SLOT_ROUTING", retry_body
        assert retry_body["replacement_count"] == 0, retry_body

        with get_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT COUNT(*) AS count
                    FROM status_history
                    WHERE opportunity_id = (
                        SELECT id FROM opportunities
                        WHERE opportunity_id = %s
                    )
                      AND event_type = 'AcceptedBidSlotRoutingRetried'
                    """,
                    (original_opportunity_id,),
                )
                assert cursor.fetchone()["count"] == 1

        with get_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT o.status, o.slot_released, o.slot_released_reason,
                           o.slot_released_by, o.slot_released_at,
                           j.status AS job_status
                    FROM opportunities o
                    JOIN jobs j ON j.opportunity_id = o.id
                    WHERE o.opportunity_id = %s
                    """,
                    (original_opportunity_id,),
                )
                original = cursor.fetchone()

                cursor.execute(
                    """
                    SELECT opportunity_id, status, contractor_id
                    FROM opportunities
                    WHERE lead_id = (
                        SELECT id FROM leads WHERE lead_id = %s
                    )
                      AND status = 'PENDING'
                    """,
                    (lead_id,),
                )
                replacements = cursor.fetchall()

                cursor.execute(
                    """
                    SELECT event_type, metadata
                    FROM status_history
                    WHERE opportunity_id = (
                        SELECT id FROM opportunities
                        WHERE opportunity_id = %s
                    )
                      AND event_type = 'AcceptedBidSlotReleased'
                    ORDER BY id DESC
                    LIMIT 1
                    """,
                    (original_opportunity_id,),
                )
                audit = cursor.fetchone()

        assert original is not None
        assert original["status"] == "ACCEPTED"
        assert original["slot_released"] is True
        assert original["slot_released_reason"] == "HTTP integration test release"
        assert original["slot_released_by"]
        assert original["slot_released_at"] is not None
        assert original["job_status"] == "BID_LOST"

        assert len(replacements) == 1
        assert replacements[0]["opportunity_id"] != original_opportunity_id
        assert replacements[0]["contractor_id"] != original_contractor_pk

        assert audit is not None
        assert audit["event_type"] == "AcceptedBidSlotReleased"
        assert audit["metadata"]["reason"] == "HTTP integration test release"

    finally:
        # Clean up only this test's lead, related records, and contractors.
        if lead_id:
            with get_connection() as connection:
                with connection.cursor() as cursor:
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
                            DELETE FROM contractors
                            WHERE contractor_id = ANY(%s)
                            """,
                            (contractor_ids,),
                        )
