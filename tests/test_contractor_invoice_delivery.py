
from datetime import date, timedelta
from decimal import Decimal

import pytest

from app.services import contractor_invoice_delivery as service


def setup_claim(monkeypatch):
    claim = {
        "delivery_id": 17,
        "claim_token": "test-claim-token",
        "recipient_email": "contractor@example.com",
    }

    loaded = {
        "invoice": {
            "invoice_number": "EMG-TEST-001",
            "billing_period_start": date(2026, 10, 1),
            "billing_period_end": date(2026, 10, 31),
            "total_amount": Decimal("1250.00"),
        },
        "contractor": {
            "company_name": "Test Contractor",
        },
        "obligations": [],
    }

    monkeypatch.setattr(
        service,
        "load_claimed_invoice_delivery",
        lambda delivery_id, claim_token: loaded,
    )

    monkeypatch.setattr(
        service,
        "render_contractor_invoice_pdf",
        lambda **kwargs: b"%PDF-test",
    )

    return claim


def raise_error(error):
    raise error


def test_invoice_delivery_success(monkeypatch):
    claim = setup_claim(monkeypatch)
    recorded = {}

    monkeypatch.setattr(
        service,
        "send_contractor_invoice_email",
        lambda **kwargs: "ses-message-123",
    )

    def record_success(delivery, message_id, sent_at, due_at):
        recorded["message_id"] = message_id
        recorded["sent_at"] = sent_at
        recorded["due_at"] = due_at
        return True

    monkeypatch.setattr(
        service,
        "_record_invoice_delivery_success",
        record_success,
    )

    result = service._deliver_claimed_invoice_delivery(claim)

    assert result == "SENT"
    assert recorded["message_id"] == "ses-message-123"
    assert recorded["due_at"].date() == (
        recorded["sent_at"].date() + timedelta(days=8)
    )
    assert recorded["due_at"].hour == 0
    assert recorded["due_at"].minute == 0


def test_pdf_failure_pauses_delivery(monkeypatch):
    claim = setup_claim(monkeypatch)
    recorded = {}

    monkeypatch.setattr(
        service,
        "render_contractor_invoice_pdf",
        lambda **kwargs: raise_error(RuntimeError("PDF generation failed")),
    )

    def record_failure(delivery, error):
        recorded["error"] = str(error)
        return True

    monkeypatch.setattr(
        service,
        "_record_invoice_delivery_failure",
        record_failure,
    )

    result = service._deliver_claimed_invoice_delivery(claim)

    assert result == "PAUSED"
    assert recorded["error"] == "PDF generation failed"


def test_email_preparation_failure_pauses_delivery(monkeypatch):
    claim = setup_claim(monkeypatch)
    recorded = {}

    error = service.InvoiceEmailPreparationError(
        "Email configuration is incomplete"
    )

    monkeypatch.setattr(
        service,
        "send_contractor_invoice_email",
        lambda **kwargs: raise_error(error),
    )

    monkeypatch.setattr(
        service,
        "_record_invoice_delivery_failure",
        lambda delivery, exc: recorded.setdefault("error", str(exc)) is not None,
    )

    result = service._deliver_claimed_invoice_delivery(claim)

    assert result == "PAUSED"
    assert recorded["error"] == str(error)


def test_definitive_ses_rejection_pauses_delivery(monkeypatch):
    claim = setup_claim(monkeypatch)
    recorded = {}

    monkeypatch.setattr(
        service,
        "send_contractor_invoice_email",
        lambda **kwargs: raise_error(RuntimeError("SES rejected the request")),
    )

    monkeypatch.setattr(
        service,
        "_is_definitive_ses_rejection",
        lambda error: True,
    )

    monkeypatch.setattr(
        service,
        "_record_invoice_delivery_failure",
        lambda delivery, error: recorded.setdefault("called", True) is True,
    )

    result = service._deliver_claimed_invoice_delivery(claim)

    assert result == "PAUSED"
    assert recorded["called"] is True


def test_uncertain_ses_outcome_becomes_unknown(monkeypatch):
    claim = setup_claim(monkeypatch)
    recorded = {}

    monkeypatch.setattr(
        service,
        "send_contractor_invoice_email",
        lambda **kwargs: raise_error(TimeoutError("SES response timed out")),
    )

    monkeypatch.setattr(
        service,
        "_is_definitive_ses_rejection",
        lambda error: False,
    )

    def record_unknown(delivery, error):
        recorded["error"] = str(error)
        return True

    monkeypatch.setattr(
        service,
        "_record_invoice_delivery_unknown",
        record_unknown,
    )

    result = service._deliver_claimed_invoice_delivery(claim)

    assert result == "UNKNOWN"
    assert "SES response timed out" in recorded["error"]


def test_database_failure_after_ses_acceptance_becomes_unknown(monkeypatch):
    claim = setup_claim(monkeypatch)
    recorded = {}

    monkeypatch.setattr(
        service,
        "send_contractor_invoice_email",
        lambda **kwargs: "ses-message-123",
    )

    monkeypatch.setattr(
        service,
        "_record_invoice_delivery_success",
        lambda *args: raise_error(
            RuntimeError("Database confirmation failed")
        ),
    )

    def record_unknown(delivery, error):
        recorded["error"] = str(error)
        return True

    monkeypatch.setattr(
        service,
        "_record_invoice_delivery_unknown",
        record_unknown,
    )

    result = service._deliver_claimed_invoice_delivery(claim)

    assert result == "UNKNOWN"
    assert "SES accepted the email" in recorded["error"]
    assert "Database confirmation failed" in recorded["error"]

def test_email_receives_correct_invoice_details_and_pdf(monkeypatch):
    claim = setup_claim(monkeypatch)
    captured = {}

    monkeypatch.setattr(
        service,
        "send_contractor_invoice_email",
        lambda **kwargs: captured.update(kwargs) or "ses-message-123",
    )

    monkeypatch.setattr(
        service,
        "_record_invoice_delivery_success",
        lambda *args: True,
    )

    result = service._deliver_claimed_invoice_delivery(claim)

    assert result == "SENT"
    assert captured["recipient_email"] == "contractor@example.com"
    assert captured["invoice_number"] == "EMG-TEST-001"
    assert captured["billing_period_start"] == "2026-10-01"
    assert captured["billing_period_end"] == "2026-10-31"
    assert captured["total_amount"] == "1,250.00"
    assert captured["pdf_bytes"] == b"%PDF-test"


def test_lost_claim_after_ses_acceptance_returns_claim_lost(monkeypatch):
    claim = setup_claim(monkeypatch)
    recorded = {}

    monkeypatch.setattr(
        service,
        "send_contractor_invoice_email",
        lambda **kwargs: "ses-message-123",
    )

    monkeypatch.setattr(
        service,
        "_record_invoice_delivery_success",
        lambda *args: False,
    )

    def record_unknown(delivery, error):
        recorded["error"] = str(error)
        return False

    monkeypatch.setattr(
        service,
        "_record_invoice_delivery_unknown",
        record_unknown,
    )

    result = service._deliver_claimed_invoice_delivery(claim)

    assert result == "CLAIM_LOST"
    assert "SES accepted the email" in recorded["error"]
    assert "claim was no longer valid" in recorded["error"]

def test_batch_processes_expired_claims_before_new_deliveries(monkeypatch):
    call_order = []

    claims = [
        {"delivery_id": 1},
        {"delivery_id": 2},
        {"delivery_id": 3},
        {"delivery_id": 4},
    ]

    outcomes = iter(["SENT", "PAUSED", "UNKNOWN", "CLAIM_LOST"])

    def mark_expired(batch_size):
        call_order.append("expired")
        assert batch_size == 10
        return [90, 91]

    def claim_due(batch_size):
        call_order.append("claim")
        assert batch_size == 10
        return claims

    monkeypatch.setattr(
        service,
        "_mark_expired_invoice_deliveries_unknown",
        mark_expired,
    )
    monkeypatch.setattr(
        service,
        "_claim_due_invoice_deliveries",
        claim_due,
    )
    monkeypatch.setattr(
        service,
        "_deliver_claimed_invoice_delivery",
        lambda claim: next(outcomes),
    )

    result = service.deliver_contractor_invoice_batch(batch_size=10)

    assert call_order == ["expired", "claim"]
    assert result == {
        "expired_marked_unknown": [90, 91],
        "claimed": 4,
        "sent": 1,
        "paused": 1,
        "unknown": 1,
        "claim_lost": 1,
        "processing_errors": 0,
    }


def test_batch_continues_after_individual_processing_exception(monkeypatch):
    claims = [
        {"delivery_id": 1},
        {"delivery_id": 2},
        {"delivery_id": 3},
    ]

    monkeypatch.setattr(
        service,
        "_mark_expired_invoice_deliveries_unknown",
        lambda batch_size: [],
    )
    monkeypatch.setattr(
        service,
        "_claim_due_invoice_deliveries",
        lambda batch_size: claims,
    )

    def process_claim(claim):
        if claim["delivery_id"] == 1:
            raise RuntimeError("Unexpected processing failure")
        if claim["delivery_id"] == 2:
            return "SENT"
        return "PAUSED"

    monkeypatch.setattr(
        service,
        "_deliver_claimed_invoice_delivery",
        process_claim,
    )

    result = service.deliver_contractor_invoice_batch()

    assert result == {
        "expired_marked_unknown": [],
        "claimed": 3,
        "sent": 1,
        "paused": 1,
        "unknown": 0,
        "claim_lost": 0,
        "processing_errors": 1,
    }