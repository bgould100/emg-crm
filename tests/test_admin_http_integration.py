import json
import os
import urllib.error
import urllib.request
import uuid

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


def test_http_admin_release_slot_creates_replacement():
    load_dotenv()
    admin_token = os.getenv("EMG_ADMIN_TOKEN")
    base_url = os.getenv("EMG_TEST_BASE_URL", "http://127.0.0.1:8001").rstrip("/")
    assert admin_token, "EMG_ADMIN_TOKEN must be configured in .env"

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

        payload = json.dumps(
            {"reason": "HTTP integration test release"}
        ).encode("utf-8")
        request = urllib.request.Request(
            (
                f"{base_url}/admin/opportunities/"
                f"{original_opportunity_id}/release-slot"
            ),
            data=payload,
            headers={
                "Content-Type": "application/json",
                "X-Admin-Token": admin_token,
            },
            method="POST",
        )

        try:
            with urllib.request.urlopen(request, timeout=15) as response:
                status_code = response.status
                response_body = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            body = error.read().decode("utf-8", errors="replace")
            raise AssertionError(
                f"HTTP endpoint returned {error.code}: {body}"
            ) from error

        # Retry through the real HTTP endpoint. The replacement is already
        # pending, so this must not create a duplicate invitation.
        retry_payload = json.dumps(
            {"reason": "HTTP integration test routing retry"}
        ).encode("utf-8")
        retry_request = urllib.request.Request(
            (
                f"{base_url}/admin/opportunities/"
                f"{original_opportunity_id}/retry-slot-routing"
            ),
            data=retry_payload,
            headers={
                "Content-Type": "application/json",
                "X-Admin-Token": admin_token,
            },
            method="POST",
        )

        try:
            with urllib.request.urlopen(retry_request, timeout=15) as response:
                retry_status_code = response.status
                retry_body = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            body = error.read().decode("utf-8", errors="replace")
            raise AssertionError(
                f"Retry HTTP endpoint returned {error.code}: {body}"
            ) from error

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
