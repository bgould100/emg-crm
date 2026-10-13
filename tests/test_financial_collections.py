from datetime import datetime, timedelta, timezone
from decimal import Decimal
import secrets

import pytest
from fastapi.testclient import TestClient

from app.api.main import app
from app.database.auth import (
    authenticate_contractor_session,
    login_contractor,
    provision_contractor_account,
)
from app.database.connection import get_connection
from app.database.contractors import create_contractor
from app.database.leads import create_lead
from app.services.matching import route_opportunities_to_fill_slots
from app.services.opportunities import respond_to_opportunity


TEST_ORIGIN = "http://localhost:8000"
TEST_PASSWORD = "EMG-Financial-Test-123!"


@pytest.fixture
def financial_job(monkeypatch):
    """Create one authenticated contractor and an accepted test job."""
    monkeypatch.setenv("EMG_ALLOWED_ORIGIN", TEST_ORIGIN)
    monkeypatch.setenv("EMG_COOKIE_SECURE", "false")

    client = TestClient(app)
    suffix = secrets.token_hex(5).upper()
    contractor_id = f"EMGF-{suffix}"
    email = f"financial-test-{suffix.lower()}@example.com"
    lead_id = None
    db_contractor_id = None

    try:
        lead = create_lead(
            zip_code="83814",
            service_type="Roofing",
            description=f"Temporary financial endpoint test {suffix}",
            requested_bid_count=1,
            customer_name=f"Financial Test {suffix}",
            customer_email=email,
        )
        lead_id = lead["lead_id"]

        contractor = create_contractor(
            company_name=f"EMG Financial Test {suffix}",
            email=email,
            service_area=["83814"],
            specialties=["Roofing"],
        )
        contractor_id = contractor["contractor_id"]

        with get_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT id FROM contractors WHERE contractor_id = %s",
                    (contractor_id,),
                )
                contractor_row = cursor.fetchone()
                assert contractor_row is not None
                db_contractor_id = contractor_row["id"]

        provision_contractor_account(
            contractor_id=contractor_id,
            email=email,
            password=TEST_PASSWORD,
        )

        login = login_contractor(email=email, password=TEST_PASSWORD)
        session = authenticate_contractor_session(login["session_token"])
        assert session is not None
        client.cookies.set("emg_session", login["session_token"])

        opportunities = route_opportunities_to_fill_slots(lead_id)
        owned_opportunity = next(
            (
                item for item in opportunities
                if item["contractor_id"] == contractor_id
            ),
            None,
        )
        assert owned_opportunity is not None, (
            "Routing did not create an opportunity for the test contractor."
        )

        accepted = respond_to_opportunity(
            owned_opportunity["opportunity_id"],
            "accept",
            db_contractor_id,
        )
        assert accepted["status"] == "ACCEPTED"

        yield {
            "client": client,
            "lead_id": lead_id,
            "job_id": accepted["job_id"],
            "contractor_id": contractor_id,
            "contractor_pk": db_contractor_id,
            "account_id": session["account_id"],
        }

    finally:
        # Remove only records associated with this fixture's lead/job.
        with get_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT id FROM leads WHERE lead_id = %s",
                    (lead_id,),
                ) if lead_id else None
                lead_row = cursor.fetchone() if lead_id else None

                if lead_row:
                    db_lead_id = lead_row["id"]

                    cursor.execute(
                        "SELECT id FROM jobs WHERE lead_id = %s",
                        (db_lead_id,),
                    )
                    job_rows = cursor.fetchall()
                    job_ids = [row["id"] for row in job_rows]

                    if job_ids:
                        cursor.execute(
                            """
                            DELETE FROM job_payment_dispute_evidence
                            WHERE dispute_id IN (
                                SELECT id FROM job_payment_disputes
                                WHERE job_id = ANY(%s)
                            )
                            """,
                            (job_ids,),
                        )
                        cursor.execute(
                            """
                            DELETE FROM homeowner_payment_verifications
                            WHERE job_id = ANY(%s)
                            """,
                            (job_ids,),
                        )
                        cursor.execute(
                            "DELETE FROM job_payment_disputes WHERE job_id = ANY(%s)",
                            (job_ids,),
                        )
                        cursor.execute(
                            "DELETE FROM job_fee_accruals WHERE job_id = ANY(%s)",
                            (job_ids,),
                        )
                        cursor.execute(
                            "DELETE FROM job_collections WHERE job_id = ANY(%s)",
                            (job_ids,),
                        )
                        cursor.execute(
                            "DELETE FROM job_payment_agreements WHERE job_id = ANY(%s)",
                            (job_ids,),
                        )
                        cursor.execute(
                            "DELETE FROM job_financial_audit_events WHERE job_id = ANY(%s)",
                            (job_ids,),
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

                if db_contractor_id is not None:
                    cursor.execute(
                        """
                        DELETE FROM contractor_sessions
                        WHERE account_id IN (
                            SELECT id FROM contractor_accounts
                            WHERE contractor_id = %s
                        )
                        """,
                        (db_contractor_id,),
                    )
                    cursor.execute(
                        "DELETE FROM contractor_accounts WHERE contractor_id = %s",
                        (db_contractor_id,),
                    )
                    cursor.execute(
                        "DELETE FROM contractors WHERE id = %s",
                        (db_contractor_id,),
                    )

        client.close()


def _report(client, job_id, amount, key, collected_at=None):
    if collected_at is None:
        collected_at = datetime.now(timezone.utc) - timedelta(minutes=1)

    return client.post(
        f"/contractor/jobs/{job_id}/collections",
        headers={
            "Origin": TEST_ORIGIN,
            "Idempotency-Key": key,
        },
        json={
            "amount": str(amount),
            "collected_at": collected_at.isoformat(),
        },
    )


def test_collection_endpoint_requires_authentication(financial_job):
    client = financial_job["client"]
    client.cookies.clear()

    response = _report(
        client,
        financial_job["job_id"],
        "100.00",
        "unauthenticated-test",
    )

    assert response.status_code == 401


def test_collection_report_creates_fee_accrual_and_is_idempotent(financial_job):
    client = financial_job["client"]
    job_id = financial_job["job_id"]

    first = _report(client, job_id, "100.00", "retry-key-1")
    assert first.status_code == 201, first.text
    first_body = first.json()

    assert first_body["job_id"] == job_id
    assert Decimal(first_body["amount"]) == Decimal("100.00")
    assert Decimal(first_body["fee_amount"]) == Decimal("10.00")
    assert Decimal(first_body["cumulative_fee_after"]) == Decimal("10.00")
    assert first_body["idempotent_replay"] is False

    retry = _report(
        client,
        job_id,
        "100.00",
        "retry-key-1",
        datetime.fromisoformat(first_body["collected_at"]),
    )
    assert retry.status_code == 201, retry.text
    assert retry.json()["idempotent_replay"] is True
    assert retry.json()["collection_id"] == first_body["collection_id"]
    assert retry.json()["fee_accrual_id"] == first_body["fee_accrual_id"]

    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT COUNT(*) AS count
                FROM job_collections
                WHERE job_id = (
                    SELECT id FROM jobs WHERE job_id = %s
                )
                """,
                (job_id,),
            )
            assert cursor.fetchone()["count"] == 1

            cursor.execute(
                """
                SELECT COUNT(*) AS count
                FROM job_fee_accruals
                WHERE job_id = (
                    SELECT id FROM jobs WHERE job_id = %s
                )
                """,
                (job_id,),
            )
            assert cursor.fetchone()["count"] == 1


def test_idempotency_key_cannot_be_reused_for_a_different_amount(financial_job):
    client = financial_job["client"]
    job_id = financial_job["job_id"]

    first = _report(client, job_id, "100.00", "conflict-key-1")
    assert first.status_code == 201, first.text

    second = _report(client, job_id, "150.00", "conflict-key-1")
    assert second.status_code == 409, second.text


def test_collection_fee_never_exceeds_five_thousand_dollars(financial_job):
    client = financial_job["client"]
    job_id = financial_job["job_id"]

    first = _report(client, job_id, "49990.00", "cap-key-1")
    assert first.status_code == 201, first.text
    assert Decimal(first.json()["fee_amount"]) == Decimal("4999.00")

    second = _report(client, job_id, "100.00", "cap-key-2")
    assert second.status_code == 201, second.text
    assert Decimal(second.json()["fee_amount"]) == Decimal("1.00")
    assert Decimal(second.json()["cumulative_fee_after"]) == Decimal("5000.00")

    third = _report(client, job_id, "100.00", "cap-key-3")
    assert third.status_code == 201, third.text
    assert Decimal(third.json()["fee_amount"]) == Decimal("0.00")
    assert Decimal(third.json()["cumulative_fee_after"]) == Decimal("5000.00")


def test_approved_nonpayment_dispute_does_not_freeze_actual_collections(financial_job):
    job_id = financial_job["job_id"]
    contractor_pk = financial_job["contractor_pk"]
    account_id = financial_job["account_id"]

    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT id
                FROM jobs
                WHERE job_id = %s
                """,
                (job_id,),
            )
            job = cursor.fetchone()
            assert job is not None

            cursor.execute(
                """
                INSERT INTO job_payment_disputes (
                    job_id, contractor_id, submitted_by_account_id,
                    reason, status, decision_notes, reviewed_by,
                    reviewed_at, fee_freeze_effective_at
                )
                VALUES (
                    %s, %s, %s,
                    'Test approved payment dispute', 'APPROVED',
                    'Approved for freeze behavior test', 'EMG Test Reviewer',
                    CURRENT_TIMESTAMP, CURRENT_TIMESTAMP - INTERVAL '1 minute'
                )
                """,
                (job["id"], contractor_pk, account_id),
            )

    old_collection_time = datetime.now(timezone.utc) - timedelta(days=3)
    response = _report(
        financial_job["client"],
        job_id,
        "100.00",
        "frozen-backdated-key",
        old_collection_time,
    )

    assert response.status_code == 201, response.text
    body = response.json()
    assert Decimal(body["fee_amount"]) == Decimal("10.00")
    assert Decimal(body["cumulative_fee_after"]) == Decimal("10.00")

    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT c.amount, a.fee_amount, a.eligible_collection_amount
                FROM job_collections c
                JOIN job_fee_accruals a ON a.collection_id = c.id
                WHERE c.id = %s
                """,
                (body["collection_id"],),
            )
            row = cursor.fetchone()

    assert row is not None
    assert Decimal(row["amount"]) == Decimal("100.00")
    assert Decimal(row["fee_amount"]) == Decimal("10.00")
    assert Decimal(row["eligible_collection_amount"]) == Decimal("100.00")


def test_contractor_cannot_report_collection_for_another_contractors_job(
    financial_job,
):
    """A valid contractor session must not grant access to another contractor's job."""
    client = financial_job["client"]
    suffix = secrets.token_hex(5).lower()
    email = f"financial-other-{suffix}@example.com"
    other_contractor_pk = None

    try:
        other_contractor = create_contractor(
            company_name=f"EMG Other Financial Test {suffix}",
            email=email,
            service_area=["83814"],
            specialties=["Roofing"],
        )
        other_contractor_id = other_contractor["contractor_id"]

        with get_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "SELECT id FROM contractors WHERE contractor_id = %s",
                    (other_contractor_id,),
                )
                row = cursor.fetchone()
                assert row is not None
                other_contractor_pk = row["id"]

        provision_contractor_account(
            contractor_id=other_contractor_id,
            email=email,
            password=TEST_PASSWORD,
        )

        login = login_contractor(email=email, password=TEST_PASSWORD)
        assert login.get("session_token")

        # Replace the fixture contractor's session with Contractor B's session.
        client.cookies.set("emg_session", login["session_token"])

        response = _report(
            client,
            financial_job["job_id"],
            "100.00",
            f"cross-owner-{suffix}",
        )

        assert response.status_code == 404, response.text

        with get_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT COUNT(*) AS count
                    FROM job_collections
                    WHERE idempotency_key = %s
                    """,
                    (f"cross-owner-{suffix}",),
                )
                assert cursor.fetchone()["count"] == 0

    finally:
        if other_contractor_pk is not None:
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
                        (other_contractor_pk,),
                    )
                    cursor.execute(
                        "DELETE FROM contractor_accounts WHERE contractor_id = %s",
                        (other_contractor_pk,),
                    )
                    cursor.execute(
                        "DELETE FROM contractors WHERE id = %s",
                        (other_contractor_pk,),
                    )


def test_collection_rejects_zero_amount(financial_job):
    response = _report(
        financial_job["client"],
        financial_job["job_id"],
        "0.00",
        "validation-zero",
    )
    assert response.status_code == 422, response.text


def test_collection_rejects_more_than_two_decimal_places(financial_job):
    response = _report(
        financial_job["client"],
        financial_job["job_id"],
        "10.001",
        "validation-decimals",
    )
    assert response.status_code == 422, response.text


def test_collection_rejects_future_collection_timestamp(financial_job):
    future = datetime.now(timezone.utc) + timedelta(days=1)
    response = _report(
        financial_job["client"],
        financial_job["job_id"],
        "100.00",
        "validation-future",
        future,
    )
    assert response.status_code == 422, response.text


def test_collection_rejects_timestamp_without_timezone(financial_job):
    naive_timestamp = datetime.now() - timedelta(minutes=1)
    response = _report(
        financial_job["client"],
        financial_job["job_id"],
        "100.00",
        "validation-no-timezone",
        naive_timestamp,
    )
    assert response.status_code == 422, response.text


def test_collection_rejects_disallowed_origin(financial_job):
    response = financial_job["client"].post(
        f"/contractor/jobs/{financial_job['job_id']}/collections",
        headers={
            "Origin": "https://not-emg.example",
            "Idempotency-Key": "validation-bad-origin",
        },
        json={
            "amount": "100.00",
            "collected_at": datetime.now(timezone.utc).isoformat(),
        },
    )
    assert response.status_code == 403, response.text


def test_collection_requires_idempotency_key(financial_job):
    response = financial_job["client"].post(
        f"/contractor/jobs/{financial_job['job_id']}/collections",
        headers={"Origin": TEST_ORIGIN},
        json={
            "amount": "100.00",
            "collected_at": datetime.now(timezone.utc).isoformat(),
        },
    )
    assert response.status_code == 422, response.text


@pytest.mark.parametrize(
    "key",
    [
        "",
        "x" * 101,
    ],
    ids=["blank-key", "key-too-long"],
)
def test_collection_rejects_invalid_idempotency_key(financial_job, key):
    response = _report(
        financial_job["client"],
        financial_job["job_id"],
        "100.00",
        key,
    )
    assert response.status_code == 422, response.text


def test_paid_in_full_agreement_can_be_created_and_retrieved(financial_job):
    client = financial_job["client"]
    job_id = financial_job["job_id"]

    response = client.post(
        f"/contractor/jobs/{job_id}/payment-agreement",
        headers={"Origin": TEST_ORIGIN},
        json={
            "arrangement_type": "PAID_IN_FULL",
            "invoice_total": "1200.00",
        },
    )
    assert response.status_code == 201, response.text
    body = response.json()

    assert body["job_id"] == job_id
    assert body["agreement"]["arrangement_type"] == "PAID_IN_FULL"
    assert Decimal(body["agreement"]["invoice_total"]) == Decimal("1200.00")
    assert body["agreement"]["version"] == 1

    retrieved = client.get(
        f"/contractor/jobs/{job_id}/payment-agreement",
        headers={"Origin": TEST_ORIGIN},
    )
    assert retrieved.status_code == 200, retrieved.text
    summary = retrieved.json()

    assert summary["agreement"]["id"] == body["agreement"]["id"]
    assert Decimal(summary["payment_summary"]["collected_total"]) == Decimal("0.00")
    assert Decimal(summary["payment_summary"]["remaining_balance"]) == Decimal("1200.00")
    assert Decimal(summary["payment_summary"]["fee_accrued"]) == Decimal("0.00")


def test_payment_plan_agreement_can_be_created(financial_job):
    client = financial_job["client"]
    job_id = financial_job["job_id"]

    response = client.post(
        f"/contractor/jobs/{job_id}/payment-agreement",
        headers={"Origin": TEST_ORIGIN},
        json={
            "arrangement_type": "PAYMENT_PLAN",
            "invoice_total": "1250.00",
            "schedule_type": "MONTHLY",
            "installment_amount": "300.00",
            "final_installment_amount": "50.00",
            "plan_start_date": "2026-10-15",
            "final_due_date": "2027-02-15",
        },
    )

    assert response.status_code == 201, response.text
    agreement = response.json()["agreement"]

    assert agreement["arrangement_type"] == "PAYMENT_PLAN"
    assert agreement["schedule_type"] == "MONTHLY"
    assert Decimal(agreement["installment_amount"]) == Decimal("300.00")
    assert Decimal(agreement["final_installment_amount"]) == Decimal("50.00")
    assert agreement["plan_start_date"] == "2026-10-15"
    assert agreement["final_due_date"] == "2027-02-15"


def test_new_agreement_versions_preserve_previous_terms(financial_job):
    client = financial_job["client"]
    job_id = financial_job["job_id"]
    url = f"/contractor/jobs/{job_id}/payment-agreement"
    headers = {"Origin": TEST_ORIGIN}

    first = client.post(
        url,
        headers=headers,
        json={
            "arrangement_type": "PAID_IN_FULL",
            "invoice_total": "900.00",
        },
    )
    assert first.status_code == 201, first.text

    second = client.post(
        url,
        headers=headers,
        json={
            "arrangement_type": "PAID_IN_FULL",
            "invoice_total": "1100.00",
        },
    )
    assert second.status_code == 201, second.text

    old_agreement = first.json()["agreement"]
    new_agreement = second.json()["agreement"]

    assert old_agreement["version"] == 1
    assert new_agreement["version"] == 2
    assert new_agreement["supersedes_agreement_id"] == old_agreement["id"]

    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT version, invoice_total
                FROM job_payment_agreements
                WHERE job_id = (
                    SELECT id FROM jobs WHERE job_id = %s
                )
                ORDER BY version
                """,
                (job_id,),
            )
            rows = cursor.fetchall()

    assert len(rows) == 2
    assert rows[0]["version"] == 1
    assert Decimal(rows[0]["invoice_total"]) == Decimal("900.00")
    assert rows[1]["version"] == 2
    assert Decimal(rows[1]["invoice_total"]) == Decimal("1100.00")


def test_payment_agreement_rejects_invalid_schedule(financial_job):
    response = financial_job["client"].post(
        f"/contractor/jobs/{financial_job['job_id']}/payment-agreement",
        headers={"Origin": TEST_ORIGIN},
        json={
            "arrangement_type": "PAYMENT_PLAN",
            "invoice_total": "1000.00",
            "schedule_type": "YEARLY",
            "installment_amount": "100.00",
            "plan_start_date": "2026-10-15",
            "final_due_date": "2027-10-15",
        },
    )

    assert response.status_code == 422, response.text


def test_payment_agreement_requires_authentication(financial_job):
    client = financial_job["client"]
    client.cookies.clear()

    response = client.post(
        f"/contractor/jobs/{financial_job['job_id']}/payment-agreement",
        headers={"Origin": TEST_ORIGIN},
        json={
            "arrangement_type": "PAID_IN_FULL",
            "invoice_total": "1000.00",
        },
    )

    assert response.status_code == 401, response.text


def test_custom_payment_schedule_accepts_matching_installments(financial_job):
    client = financial_job["client"]
    job_id = financial_job["job_id"]

    response = client.post(
        f"/contractor/jobs/{job_id}/payment-agreement",
        headers={"Origin": TEST_ORIGIN},
        json={
            "arrangement_type": "PAYMENT_PLAN",
            "invoice_total": "1000.00",
            "schedule_type": "CUSTOM",
            "installment_amount": "500.00",
            "final_installment_amount": "500.00",
            "plan_start_date": "2026-10-15",
            "final_due_date": "2026-11-15",
            "custom_schedule": [
                {"due_date": "2026-10-15", "amount": "500.00"},
                {"due_date": "2026-11-15", "amount": "500.00"},
            ],
        },
    )

    assert response.status_code == 201, response.text
    agreement = response.json()["agreement"]
    assert agreement["schedule_type"] == "CUSTOM"
    assert len(agreement["custom_schedule"]) == 2


def test_custom_payment_schedule_rejects_incorrect_total(financial_job):
    response = financial_job["client"].post(
        f"/contractor/jobs/{financial_job['job_id']}/payment-agreement",
        headers={"Origin": TEST_ORIGIN},
        json={
            "arrangement_type": "PAYMENT_PLAN",
            "invoice_total": "1000.00",
            "schedule_type": "CUSTOM",
            "installment_amount": "500.00",
            "plan_start_date": "2026-10-15",
            "final_due_date": "2026-11-15",
            "custom_schedule": [
                {"due_date": "2026-10-15", "amount": "400.00"},
                {"due_date": "2026-11-15", "amount": "500.00"},
            ],
        },
    )

    assert response.status_code == 422, response.text
    assert "add up to the invoice total" in response.json()["detail"]


def test_custom_payment_schedule_rejects_duplicate_dates(financial_job):
    response = financial_job["client"].post(
        f"/contractor/jobs/{financial_job['job_id']}/payment-agreement",
        headers={"Origin": TEST_ORIGIN},
        json={
            "arrangement_type": "PAYMENT_PLAN",
            "invoice_total": "1000.00",
            "schedule_type": "CUSTOM",
            "installment_amount": "500.00",
            "plan_start_date": "2026-10-15",
            "final_due_date": "2026-11-15",
            "custom_schedule": [
                {"due_date": "2026-10-15", "amount": "500.00"},
                {"due_date": "2026-10-15", "amount": "500.00"},
            ],
        },
    )

    assert response.status_code == 422, response.text
    assert "strictly increasing" in response.json()["detail"]


def test_payment_summary_updates_after_collection(financial_job):
    client = financial_job["client"]
    job_id = financial_job["job_id"]
    headers = {"Origin": TEST_ORIGIN}

    agreement_response = client.post(
        f"/contractor/jobs/{job_id}/payment-agreement",
        headers=headers,
        json={
            "arrangement_type": "PAID_IN_FULL",
            "invoice_total": "500.00",
        },
    )
    assert agreement_response.status_code == 201, agreement_response.text

    collection_response = client.post(
        f"/contractor/jobs/{job_id}/collections",
        headers={
            **headers,
            "Idempotency-Key": "payment-summary-collection-test",
        },
        json={
            "amount": "200.00",
            "collected_at": "2026-10-01T12:00:00Z",
        },
    )
    assert collection_response.status_code == 201, collection_response.text

    summary_response = client.get(
        f"/contractor/jobs/{job_id}/payment-agreement",
        headers=headers,
    )
    assert summary_response.status_code == 200, summary_response.text

    summary = summary_response.json()["payment_summary"]
    assert Decimal(summary["collected_total"]) == Decimal("200.00")
    assert Decimal(summary["remaining_balance"]) == Decimal("300.00")
    assert Decimal(summary["fee_accrued"]) == Decimal("20.00")
