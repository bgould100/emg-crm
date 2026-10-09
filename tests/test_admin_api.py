
import app.api.main as api
from fastapi.testclient import TestClient


client = TestClient(api.app)


def test_admin_controls_fail_closed_without_configuration(monkeypatch):
    monkeypatch.delenv("EMG_ADMIN_TOKEN", raising=False)

    response = client.post(
        "/admin/opportunities/EMGO-TEST/hold",
        json={"reason": "Testing missing configuration"},
    )

    assert response.status_code == 503


def test_admin_controls_reject_missing_and_wrong_tokens(monkeypatch):
    monkeypatch.setenv("EMG_ADMIN_TOKEN", "test-secret-token")

    missing = client.post(
        "/admin/opportunities/EMGO-TEST/hold",
        json={"reason": "Testing missing token"},
    )
    wrong = client.post(
        "/admin/opportunities/EMGO-TEST/hold",
        headers={"X-Admin-Token": "incorrect-token"},
        json={"reason": "Testing incorrect token"},
    )

    assert missing.status_code == 401
    assert wrong.status_code == 401


def test_admin_hold_endpoint(monkeypatch):
    monkeypatch.setenv("EMG_ADMIN_TOKEN", "test-secret-token")
    calls = []

    def fake_hold(opportunity_id, *, reason, performed_by):
        calls.append((opportunity_id, reason, performed_by))

    monkeypatch.setattr(api, "hold_opportunity", fake_hold)

    response = client.post(
        "/admin/opportunities/EMGO-TEST/hold",
        headers={"X-Admin-Token": "test-secret-token"},
        json={"reason": "Contractor needs more time"},
    )

    assert response.status_code == 200
    assert response.json()["action"] == "HOLD"
    assert calls == [
        (
            "EMGO-TEST",
            "Contractor needs more time",
            "local-admin",
        )
    ]


def test_admin_extend_endpoint(monkeypatch):
    monkeypatch.setenv("EMG_ADMIN_TOKEN", "test-secret-token")
    calls = []

    def fake_extend(
        opportunity_id, *, days, reason, performed_by
    ):
        calls.append((opportunity_id, days, reason, performed_by))

    monkeypatch.setattr(
        api, "extend_opportunity_deadline", fake_extend
    )

    response = client.post(
        "/admin/opportunities/EMGO-TEST/extend",
        headers={"X-Admin-Token": "test-secret-token"},
        json={
            "days": 3,
            "reason": "Contractor requested an extension",
        },
    )

    assert response.status_code == 200
    assert response.json()["action"] == "EXTEND"
    assert calls == [
        (
            "EMGO-TEST",
            3,
            "Contractor requested an extension",
            "local-admin",
        )
    ]


def test_admin_release_hold_endpoint(monkeypatch):
    monkeypatch.setenv("EMG_ADMIN_TOKEN", "test-secret-token")
    calls = []

    def fake_release(opportunity_id, *, reason, performed_by):
        calls.append((opportunity_id, reason, performed_by))

    monkeypatch.setattr(
        api, "release_opportunity_hold", fake_release
    )

    response = client.post(
        "/admin/opportunities/EMGO-TEST/release-hold",
        headers={"X-Admin-Token": "test-secret-token"},
        json={"reason": "Administrative review completed"},
    )

    assert response.status_code == 200
    assert response.json()["action"] == "RELEASE_HOLD"
    assert calls == [
        (
            "EMGO-TEST",
            "Administrative review completed",
            "local-admin",
        )
    ]