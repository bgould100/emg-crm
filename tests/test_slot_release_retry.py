import pytest

from app.database.connection import get_connection
from app.database.contractors import create_contractor
from app.database.leads import create_lead
from app.services.matching import route_opportunities_to_fill_slots
from app.services.opportunities import respond_to_opportunity
from app.services.job_status import update_job_status
import app.services.slot_release as slot_release


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


def test_routing_failure_can_be_retried_without_releasing_twice(monkeypatch):
    lead_id = None
    contractor_ids = []
    original_router = slot_release.route_opportunities_to_fill_slots

    try:
        lead = create_lead(
            zip_code="83814",
            service_type="Roofing",
            description="Test retry after replacement routing failure.",
            requested_bid_count=1,
            customer_name="Slot Retry Test",
            customer_email="slot-retry-test@example.com",
        )
        lead_id = lead["lead_id"]

        for number in range(3):
            contractor = create_contractor(
                company_name=f"EMG Slot Retry Test {number}",
                email=f"slot-retry-test-{number}@example.com",
                service_area=["83814"],
                specialties=["Roofing"],
            )
            contractor_ids.append(contractor["contractor_id"])

        initial = route_opportunities_to_fill_slots(lead_id)
        assert len(initial) == 1

        original_id = initial[0]["opportunity_id"]
        original_contractor_pk = _contractor_pk(original_id)

        accepted = respond_to_opportunity(
            original_id, "accept", original_contractor_pk
        )
        assert accepted["status"] == "ACCEPTED"

        updated_job = update_job_status(
            accepted["job_id"], "BID_LOST", original_contractor_pk
        )
        assert updated_job["status"] == "BID_LOST"

        def fail_routing(_lead_id):
            raise RuntimeError("Simulated routing failure")

        monkeypatch.setattr(
            slot_release, "route_opportunities_to_fill_slots", fail_routing
        )

        with pytest.raises(RuntimeError, match="Simulated routing failure"):
            slot_release.release_accepted_bid_slot(
                original_id,
                reason="Contractor accepted in error",
                performed_by="slot-retry-test-admin",
            )

        # The release must remain committed despite routing failure.
        with get_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT slot_released, status
                    FROM opportunities
                    WHERE opportunity_id = %s
                    """,
                    (original_id,),
                )
                original = cursor.fetchone()

                cursor.execute(
                    """
                    SELECT COUNT(*) AS count
                    FROM opportunities
                    WHERE lead_id = (SELECT id FROM leads WHERE lead_id = %s)
                      AND status = 'PENDING'
                    """,
                    (lead_id,),
                )
                pending_count = cursor.fetchone()["count"]

        assert original["slot_released"] is True
        assert original["status"] == "ACCEPTED"
        assert pending_count == 0

        # Restore the real router and retry without releasing the slot again.
        monkeypatch.setattr(
            slot_release, "route_opportunities_to_fill_slots", original_router
        )
        result = slot_release.retry_replacement_routing(
            original_id,
            reason="Retry after temporary routing failure",
            performed_by="slot-retry-test-admin",
        )

        assert result["action"] == "RETRY_SLOT_ROUTING"
        assert result["replacement_count"] == 1

        replacement = result["replacement_opportunities"][0]
        assert replacement["opportunity_id"] != original_id
        assert replacement["contractor_id"] != original_contractor_pk

        # A further retry should not create another invitation.
        second_retry = slot_release.retry_replacement_routing(
            original_id,
            reason="Verify safe repeated retry",
            performed_by="slot-retry-test-admin",
        )
        assert second_retry["replacement_count"] == 0

        with get_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT COUNT(*) AS count
                    FROM opportunities
                    WHERE lead_id = (SELECT id FROM leads WHERE lead_id = %s)
                      AND status = 'PENDING'
                    """,
                    (lead_id,),
                )
                assert cursor.fetchone()["count"] == 1

                cursor.execute(
                    """
                    SELECT COUNT(*) AS count
                    FROM status_history
                    WHERE opportunity_id = (
                        SELECT id FROM opportunities WHERE opportunity_id = %s
                    )
                      AND event_type = 'AcceptedBidSlotReleased'
                    """,
                    (original_id,),
                )
                assert cursor.fetchone()["count"] == 1

                cursor.execute(
                    """
                    SELECT COUNT(*) AS count
                    FROM status_history
                    WHERE opportunity_id = (
                        SELECT id FROM opportunities WHERE opportunity_id = %s
                    )
                      AND event_type = 'AcceptedBidSlotRoutingRetried'
                    """,
                    (original_id,),
                )
                assert cursor.fetchone()["count"] == 2

    finally:
        if lead_id:
            with get_connection() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        "SELECT id FROM leads WHERE lead_id = %s", (lead_id,)
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
                            "DELETE FROM leads WHERE id = %s", (db_lead_id,)
                        )

                    if contractor_ids:
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
                            "DELETE FROM contractors WHERE contractor_id = ANY(%s)",
                            (contractor_ids,),
                        )
