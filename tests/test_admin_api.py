
import app.api.main as api
from fastapi.testclient import TestClient


client = TestClient(api.app)


# -------------------------
# Admin authentication
# -------------------------

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


# -------------------------
# Admin opportunity controls
# -------------------------

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
        opportunity_id,
        *,
        days,
        reason,
        performed_by,
    ):
        calls.append(
            (opportunity_id, days, reason, performed_by)
        )

    monkeypatch.setattr(
        api,
        "extend_opportunity_deadline",
        fake_extend,
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
        api,
        "release_opportunity_hold",
        fake_release,
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


# -------------------------
# Accepted bid-slot release
# -------------------------

def test_admin_release_accepted_slot_endpoint(monkeypatch):
    monkeypatch.setenv("EMG_ADMIN_TOKEN", "test-secret-token")
    monkeypatch.setenv("EMG_ADMIN_IDENTITY", "local-admin")
    calls = []

    def fake_release(opportunity_id, *, reason, performed_by):
        calls.append((opportunity_id, reason, performed_by))
        return {
            "opportunity_id": opportunity_id,
            "action": "SLOT_RELEASED",
            "slot_released": True,
            "replacement_count": 2,
            "replacement_opportunities": ["EMGO-REPLACEMENT-1", "EMGO-REPLACEMENT-2"],
        }

    monkeypatch.setattr(api, "release_accepted_bid_slot", fake_release)

    response = client.post(
        "/admin/opportunities/EMGO-TEST/release-slot",
        headers={"X-Admin-Token": "test-secret-token"},
        json={"reason": "Accepted by mistake"},
    )

    assert response.status_code == 200
    assert response.json()["action"] == "SLOT_RELEASED"
    assert response.json()["replacement_count"] == 2
    assert "2 replacement invitation(s) created" in response.json()["message"]
    assert calls == [
        ("EMGO-TEST", "Accepted by mistake", "local-admin")
    ]


def test_admin_release_accepted_slot_rejects_missing_and_wrong_tokens(monkeypatch):
    monkeypatch.setenv("EMG_ADMIN_TOKEN", "test-secret-token")
    calls = []

    def fake_release(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("Release service must not be called.")

    monkeypatch.setattr(api, "release_accepted_bid_slot", fake_release)

    missing = client.post(
        "/admin/opportunities/EMGO-TEST/release-slot",
        json={"reason": "Accepted by mistake"},
    )
    wrong = client.post(
        "/admin/opportunities/EMGO-TEST/release-slot",
        headers={"X-Admin-Token": "wrong-token"},
        json={"reason": "Accepted by mistake"},
    )

    assert missing.status_code == 401
    assert wrong.status_code == 401
    assert calls == []


def test_admin_release_accepted_slot_maps_service_conflict_to_409(monkeypatch):
    monkeypatch.setenv("EMG_ADMIN_TOKEN", "test-secret-token")

    def fake_release(opportunity_id, *, reason, performed_by):
        raise ValueError("The accepted bid slot has already been released.")

    monkeypatch.setattr(api, "release_accepted_bid_slot", fake_release)

    response = client.post(
        "/admin/opportunities/EMGO-TEST/release-slot",
        headers={"X-Admin-Token": "test-secret-token"},
        json={"reason": "Accepted by mistake"},
    )

    assert response.status_code == 409
    assert "already been released" in response.json()["detail"]


# -------------------------
# Contractor account provisioning security
# -------------------------

def test_account_provisioning_rejects_missing_admin_token(monkeypatch):
    monkeypatch.setenv("EMG_ADMIN_TOKEN", "test-secret-token")
    calls = []

    def fake_provision(**kwargs):
        calls.append(kwargs)
        raise AssertionError("Provisioning must not be called.")

    monkeypatch.setattr(
        api,
        "provision_contractor_account",
        fake_provision,
    )

    response = client.post(
        "/admin/contractor-accounts",
        json={
            "contractor_id": "EMGC-TEST",
            "email": "contractor@example.com",
            "password": "test-password-123",
        },
    )

    assert response.status_code == 401
    assert calls == []


def test_account_provisioning_rejects_wrong_admin_token(monkeypatch):
    monkeypatch.setenv("EMG_ADMIN_TOKEN", "test-secret-token")
    calls = []

    def fake_provision(**kwargs):
        calls.append(kwargs)
        raise AssertionError("Provisioning must not be called.")

    monkeypatch.setattr(
        api,
        "provision_contractor_account",
        fake_provision,
    )

    response = client.post(
        "/admin/contractor-accounts",
        headers={"X-Admin-Token": "wrong-token"},
        json={
            "contractor_id": "EMGC-TEST",
            "email": "contractor@example.com",
            "password": "test-password-123",
        },
    )

    assert response.status_code == 401
    assert calls == []


# -------------------------
# Lead routing security
# -------------------------

def test_lead_routing_rejects_missing_admin_token(monkeypatch):
    monkeypatch.setenv("EMG_ADMIN_TOKEN", "test-secret-token")
    calls = []

    def fake_route(lead_id):
        calls.append(lead_id)
        raise AssertionError("Routing must not be called.")

    monkeypatch.setattr(
        api,
        "route_opportunities_to_fill_slots",
        fake_route,
    )

    response = client.post("/leads/EMGL-TEST/route")

    assert response.status_code == 401
    assert calls == []


def test_lead_routing_rejects_wrong_admin_token(monkeypatch):
    monkeypatch.setenv("EMG_ADMIN_TOKEN", "test-secret-token")
    calls = []

    def fake_route(lead_id):
        calls.append(lead_id)
        raise AssertionError("Routing must not be called.")

    monkeypatch.setattr(
        api,
        "route_opportunities_to_fill_slots",
        fake_route,
    )

    response = client.post(
        "/leads/EMGL-TEST/route",
        headers={"X-Admin-Token": "wrong-token"},
    )

    assert response.status_code == 401
    assert calls == []


# -------------------------
# Accepted bid-slot routing retry
# -------------------------

def test_admin_retry_slot_routing_endpoint(monkeypatch):
    monkeypatch.setenv("EMG_ADMIN_TOKEN", "test-secret-token")
    monkeypatch.setenv("EMG_ADMIN_IDENTITY", "local-admin")
    calls = []

    def fake_retry(opportunity_id, *, reason, performed_by):
        calls.append((opportunity_id, reason, performed_by))
        return {
            "opportunity_id": opportunity_id,
            "action": "RETRY_SLOT_ROUTING",
            "replacement_count": 1,
            "replacement_opportunities": ["EMGO-REPLACEMENT-1"],
        }

    monkeypatch.setattr(api, "retry_replacement_routing", fake_retry)

    response = client.post(
        "/admin/opportunities/EMGO-TEST/retry-slot-routing",
        headers={"X-Admin-Token": "test-secret-token"},
        json={"reason": "Retry after routing failure"},
    )

    assert response.status_code == 200
    assert response.json()["action"] == "RETRY_SLOT_ROUTING"
    assert response.json()["replacement_count"] == 1
    assert "1 replacement invitation(s) created" in response.json()["message"]
    assert calls == [
        ("EMGO-TEST", "Retry after routing failure", "local-admin")
    ]


def test_admin_retry_slot_routing_rejects_missing_and_wrong_tokens(monkeypatch):
    monkeypatch.setenv("EMG_ADMIN_TOKEN", "test-secret-token")
    calls = []

    def fake_retry(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("Retry service must not be called.")

    monkeypatch.setattr(api, "retry_replacement_routing", fake_retry)

    missing = client.post(
        "/admin/opportunities/EMGO-TEST/retry-slot-routing",
        json={"reason": "Retry after routing failure"},
    )
    wrong = client.post(
        "/admin/opportunities/EMGO-TEST/retry-slot-routing",
        headers={"X-Admin-Token": "wrong-token"},
        json={"reason": "Retry after routing failure"},
    )

    assert missing.status_code == 401
    assert wrong.status_code == 401
    assert calls == []


def test_admin_retry_slot_routing_maps_service_errors(monkeypatch):
    monkeypatch.setenv("EMG_ADMIN_TOKEN", "test-secret-token")

    def fake_retry(opportunity_id, *, reason, performed_by):
        if opportunity_id == "EMGO-MISSING":
            raise LookupError("Opportunity not found.")
        raise ValueError("Slot has not been released.")

    monkeypatch.setattr(api, "retry_replacement_routing", fake_retry)

    headers = {"X-Admin-Token": "test-secret-token"}
    body = {"reason": "Retry after routing failure"}

    missing = client.post(
        "/admin/opportunities/EMGO-MISSING/retry-slot-routing",
        headers=headers,
        json=body,
    )
    conflict = client.post(
        "/admin/opportunities/EMGO-TEST/retry-slot-routing",
        headers=headers,
        json=body,
    )

    assert missing.status_code == 404
    assert "Opportunity not found" in missing.json()["detail"]
    assert conflict.status_code == 409
    assert "Slot has not been released" in conflict.json()["detail"]
