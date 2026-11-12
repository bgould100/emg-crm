
import logging

import pytest

from app.workers import homeowner_review_delivery_worker as worker


@pytest.fixture
def worker_signals(monkeypatch):
    monkeypatch.setattr(worker.signal, "signal", lambda *args: None)


def stop_after_one_cycle(monkeypatch):
    def wait(_interval):
        worker.stop_event.set()
        return True

    monkeypatch.setattr(worker.stop_event, "wait", wait)


def test_worker_runs_delivery_and_expiration(monkeypatch, worker_signals):
    stop_after_one_cycle(monkeypatch)
    calls = []

    monkeypatch.setattr(
        worker,
        "deliver_homeowner_review_invitations",
        lambda: calls.append("delivery") or {
            "claimed_count": 2,
            "sent_count": 2,
            "failed_count": 0,
            "paused_invitation_ids": [],
            "claim_lost_invitation_ids": [],
            "unknown_invitation_ids": [],
            "recovered_unknown_invitation_ids": [],
        },
    )
    monkeypatch.setattr(
        worker,
        "expire_homeowner_review_invitations",
        lambda: calls.append("expiration") or {
            "expired_count": 1,
            "expired_invitation_ids": [123],
        },
    )

    worker.run_worker()

    assert calls == ["delivery", "expiration"]


def test_worker_runs_expiration_when_delivery_fails(
    monkeypatch, worker_signals, caplog
):
    stop_after_one_cycle(monkeypatch)
    calls = []

    def fail_delivery():
        calls.append("delivery")
        raise RuntimeError("delivery failed")

    def expire():
        calls.append("expiration")
        return {"expired_count": 0, "expired_invitation_ids": []}

    monkeypatch.setattr(worker, "deliver_homeowner_review_invitations", fail_delivery)
    monkeypatch.setattr(worker, "expire_homeowner_review_invitations", expire)

    with caplog.at_level(logging.ERROR):
        worker.run_worker()

    assert calls == ["delivery", "expiration"]
    assert "Unexpected homeowner review delivery worker error." in caplog.text


def test_worker_runs_delivery_when_expiration_fails(
    monkeypatch, worker_signals, caplog
):
    stop_after_one_cycle(monkeypatch)
    calls = []

    def deliver():
        calls.append("delivery")
        return {
            "claimed_count": 0,
            "sent_count": 0,
            "failed_count": 0,
            "paused_invitation_ids": [],
            "claim_lost_invitation_ids": [],
            "unknown_invitation_ids": [],
            "recovered_unknown_invitation_ids": [],
        }

    def fail_expiration():
        calls.append("expiration")
        raise RuntimeError("expiration failed")

    monkeypatch.setattr(worker, "deliver_homeowner_review_invitations", deliver)
    monkeypatch.setattr(
        worker, "expire_homeowner_review_invitations", fail_expiration
    )

    with caplog.at_level(logging.ERROR):
        worker.run_worker()

    assert calls == ["delivery", "expiration"]
    assert "Unexpected homeowner review expiration worker error." in caplog.text


def test_request_shutdown_sets_stop_event():
    worker.stop_event.clear()

    worker.request_shutdown(None, None)

    assert worker.stop_event.is_set()
    worker.stop_event.clear()
