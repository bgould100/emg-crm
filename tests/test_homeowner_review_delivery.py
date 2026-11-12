
import pytest

from app.services import homeowner_review_delivery as service


def make_invitation(invitation_id=41):
    return {
        "id": invitation_id,
        "customer_email": "homeowner@example.com",
        "token": "test-token",
    }


def prepare_delivery(monkeypatch, invitations=None):
    invitations = invitations or [make_invitation()]

    monkeypatch.setattr(
        service, "_recover_expired_review_claims", lambda: [99]
    )
    monkeypatch.setattr(
        service,
        "_email_configuration",
        lambda: (
            "reviews@example.com",
            "https://example.com",
            object(),
        ),
    )
    monkeypatch.setattr(
        service, "_claim_due_invitations", lambda batch_size: invitations
    )


def test_delivery_records_success(monkeypatch):
    prepare_delivery(monkeypatch)

    monkeypatch.setattr(
        service, "_send_invitation", lambda *args: "ses-message-123"
    )
    monkeypatch.setattr(
        service, "_record_success", lambda invitation, message_id: True
    )

    result = service.deliver_homeowner_review_invitations()

    assert result["claimed_count"] == 1
    assert result["sent_count"] == 1
    assert result["sent_invitation_ids"] == [41]
    assert result["unknown_invitation_ids"] == []
    assert result["recovered_unknown_invitation_ids"] == [99]


def test_definitive_ses_rejection_records_failure(monkeypatch):
    prepare_delivery(monkeypatch)

    error = RuntimeError("SES rejected the request")

    def send_failure(*args):
        raise error

    monkeypatch.setattr(service, "_send_invitation", send_failure)
    monkeypatch.setattr(
        service, "_is_definitive_ses_rejection", lambda error: True
    )
    monkeypatch.setattr(
        service,
        "_record_failure",
        lambda invitation, error: {
            "recorded": True,
            "failure_streak": 1,
            "paused": False,
        },
    )

    result = service.deliver_homeowner_review_invitations()

    assert result["failed_count"] == 1
    assert result["failed_invitations"] == [
        {
            "invitation_id": 41,
            "failure_streak": 1,
            "paused": False,
        }
    ]
    assert result["unknown_invitation_ids"] == []
    assert result["sent_count"] == 0


def test_uncertain_ses_error_records_unknown_without_retry(monkeypatch):
    prepare_delivery(monkeypatch)

    def send_timeout(*args):
        raise TimeoutError("SES response timed out")

    monkeypatch.setattr(service, "_send_invitation", send_timeout)
    monkeypatch.setattr(
        service, "_is_definitive_ses_rejection", lambda error: False
    )

    recorded = []
    monkeypatch.setattr(
        service,
        "_record_unknown",
        lambda invitation, error: recorded.append(
            (invitation["id"], error)
        ) or True,
    )

    failure_calls = []
    monkeypatch.setattr(
        service,
        "_record_failure",
        lambda *args: failure_calls.append(args),
    )

    result = service.deliver_homeowner_review_invitations()

    assert result["unknown_invitation_ids"] == [41]
    assert result["failed_count"] == 0
    assert result["sent_count"] == 0
    assert recorded[0][0] == 41
    assert isinstance(recorded[0][1], TimeoutError)
    assert failure_calls == []


def test_database_confirmation_failure_after_ses_acceptance_records_unknown(
    monkeypatch,
):
    prepare_delivery(monkeypatch)

    monkeypatch.setattr(
        service, "_send_invitation", lambda *args: "ses-message-accepted"
    )

    def record_success_failure(invitation, message_id):
        assert message_id == "ses-message-accepted"
        raise RuntimeError("database unavailable")

    unknown_errors = []

    def record_unknown(invitation, error):
        unknown_errors.append(error)
        return True

    monkeypatch.setattr(
        service, "_record_success", record_success_failure
    )
    monkeypatch.setattr(service, "_record_unknown", record_unknown)

    result = service.deliver_homeowner_review_invitations()

    assert result["unknown_invitation_ids"] == [41]
    assert result["sent_count"] == 0
    assert result["failed_count"] == 0
    assert len(unknown_errors) == 1
    assert "SES accepted message ses-message-accepted" in str(
        unknown_errors[0]
    )
    assert "database unavailable" in str(unknown_errors[0])


def test_delivery_reports_lost_success_claim(monkeypatch):
    prepare_delivery(monkeypatch)

    monkeypatch.setattr(
        service, "_send_invitation", lambda *args: "ses-message-123"
    )
    monkeypatch.setattr(
        service, "_record_success", lambda invitation, message_id: False
    )

    result = service.deliver_homeowner_review_invitations()

    assert result["claim_lost_invitation_ids"] == [41]
    assert result["sent_count"] == 0


def test_delivery_rejects_invalid_batch_size(monkeypatch):
    with pytest.raises(ValueError):
        service.deliver_homeowner_review_invitations(batch_size=0)

    with pytest.raises(ValueError):
        service.deliver_homeowner_review_invitations(
            batch_size=service.MAX_BATCH_SIZE + 1
        )


from datetime import datetime, timedelta, timezone
import json


class FakeCursor:
    def __init__(self, invitation):
        self.invitation = invitation
        self.queries = []
        self.result = None

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, sql, params=None):
        self.queries.append((sql, params))

        if "SELECT" in sql and "FOR UPDATE" in sql:
            self.result = self.invitation
        elif "UPDATE homeowner_review_invitations" in sql:
            self.result = {
                "attempts": self.invitation["attempts"],
                "failure_streak": self.invitation["failure_streak"],
            }
        else:
            self.result = None

    def fetchone(self):
        return self.result


class FakeConnection:
    def __init__(self, invitation):
        self.cursor_instance = FakeCursor(invitation)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def cursor(self):
        return self.cursor_instance


def make_unknown_invitation(**overrides):
    invitation = {
        "id": 41,
        "delivery_status": "UNKNOWN",
        "retry_state": "UNKNOWN",
        "attempts": 2,
        "failure_streak": 1,
        "expires_at": datetime.now(timezone.utc) + timedelta(days=7),
        "submitted_at": None,
    }
    invitation.update(overrides)
    return invitation


def fake_connection(monkeypatch, invitation):
    connection = FakeConnection(invitation)
    monkeypatch.setattr(
        service, "get_connection", lambda: connection
    )
    return connection


def test_resolve_unknown_invitation_resend(monkeypatch):
    invitation = make_unknown_invitation()
    connection = fake_connection(monkeypatch, invitation)

    result = service.resolve_homeowner_review_invitation(
        invitation_id=41,
        admin_id=7,
        action="RESEND",
        reason="Confirmed homeowner still needs invitation",
    )

    assert result["delivery_status"] == "PENDING"
    assert result["retry_state"] == "ACTIVE"

    queries = connection.cursor_instance.queries
    update_sql = next(
        sql for sql, _ in queries
        if "UPDATE homeowner_review_invitations" in sql
    )
    assert "failure_streak = 0" in update_sql
    assert "next_attempt_at = CURRENT_TIMESTAMP" in update_sql
    assert "retry_control_admin_id" in update_sql

    event_sql = next(
        sql for sql, _ in queries
        if "INSERT INTO homeowner_review_delivery_events" in sql
    )
    assert "ADMIN_RESOLVED" in event_sql
    assert "ADMIN_RESEND" in event_sql

    event_params = next(
        params for sql, params in queries
        if "INSERT INTO homeowner_review_delivery_events" in sql
    )
    assert "Confirmed homeowner still needs invitation" in event_params[4]
    assert "duplicate_delivery_possible" in event_params[4]


def test_resolve_unknown_invitation_do_not_resend(monkeypatch):
    invitation = make_unknown_invitation()
    connection = fake_connection(monkeypatch, invitation)

    result = service.resolve_homeowner_review_invitation(
        invitation_id=41,
        admin_id=7,
        action="DO_NOT_RESEND",
        reason="Homeowner confirmed receipt",
    )

    assert result["delivery_status"] == "UNKNOWN"
    assert result["retry_state"] == "COMPLETE"

    update_sql = next(
        sql for sql, _ in connection.cursor_instance.queries
        if "UPDATE homeowner_review_invitations" in sql
    )
    assert "retry_state = 'COMPLETE'" in update_sql
    assert "next_attempt_at = NULL" in update_sql

    event_params = next(
        params for sql, params in connection.cursor_instance.queries
        if "INSERT INTO homeowner_review_delivery_events" in sql
    )
    assert "Homeowner confirmed receipt" in event_params[4]
    assert "delivery_status_remains" in event_params[4]


@pytest.mark.parametrize(
    "overrides",
    [
        {"submitted_at": datetime.now(timezone.utc)},
        {"delivery_status": "SENT"},
        {"retry_state": "ACTIVE"},
        {"expires_at": datetime.now(timezone.utc) - timedelta(days=1)},
    ],
)
def test_resolve_unknown_invitation_rejects_invalid_state(
    monkeypatch, overrides
):
    invitation = make_unknown_invitation(**overrides)
    fake_connection(monkeypatch, invitation)

    with pytest.raises(service.ReviewInvitationConflictError):
        service.resolve_homeowner_review_invitation(
            invitation_id=41,
            admin_id=7,
            action="RESEND",
            reason="Retry requested by administrator",
        )


def test_resolve_unknown_invitation_rejects_missing_invitation(
    monkeypatch,
):
    connection = fake_connection(monkeypatch, make_unknown_invitation())

    connection.cursor_instance.invitation = None

    with pytest.raises(service.ReviewInvitationNotFoundError):
        service.resolve_homeowner_review_invitation(
            invitation_id=999,
            admin_id=7,
            action="RESEND",
            reason="Retry requested by administrator",
        )


def test_resolve_unknown_invitation_rejects_invalid_action(
    monkeypatch,
):
    fake_connection(monkeypatch, make_unknown_invitation())

    with pytest.raises(ValueError):
        service.resolve_homeowner_review_invitation(
            invitation_id=41,
            admin_id=7,
            action="RETRY",
            reason="Retry requested by administrator",
        )


class RecoveryCursor:
    def __init__(self, invitations, updated=None):
        self.invitations = invitations
        self.updated = updated
        self.queries = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, sql, params=None):
        self.queries.append((sql, params))

    def fetchall(self):
        return self.invitations

    def fetchone(self):
        return self.updated


class RecoveryConnection:
    def __init__(self, cursor):
        self.cursor_instance = cursor

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def cursor(self):
        return self.cursor_instance

def test_recover_expired_claim_marks_delivery_unknown(monkeypatch):
    claimed_until = datetime.now(timezone.utc) - timedelta(minutes=1)
    invitation = {
        "id": 51,
        "attempts": 2,
        "failure_streak": 1,
        "claimed_until": claimed_until,
    }
    updated = {"attempts": 2, "failure_streak": 1}
    cursor = RecoveryCursor([invitation], updated)
    connection = RecoveryConnection(cursor)

    monkeypatch.setattr(service, "get_connection", lambda: connection)

    recovered = service._recover_expired_review_claims()

    assert recovered == [51]
    assert len(cursor.queries) == 3

    recovery_sql, recovery_params = cursor.queries[1]
    assert "SET delivery_status = 'UNKNOWN'" in recovery_sql
    assert "retry_state = 'UNKNOWN'" in recovery_sql
    assert "claimed_until = NULL" in recovery_sql
    assert "OUTCOME_UNKNOWN" in cursor.queries[2][0]
    assert recovery_params[1] == 51
    assert recovery_params[2] == claimed_until


def test_recover_expired_claim_skips_lost_update(monkeypatch):
    invitation = {
        "id": 52,
        "attempts": 1,
        "failure_streak": 0,
        "claimed_until": datetime.now(timezone.utc) - timedelta(minutes=1),
    }
    cursor = RecoveryCursor([invitation], updated=None)
    connection = RecoveryConnection(cursor)

    monkeypatch.setattr(service, "get_connection", lambda: connection)

    recovered = service._recover_expired_review_claims()

    assert recovered == []
    assert len(cursor.queries) == 2


def test_recover_expired_claim_rejects_invalid_batch_size():
    for batch_size in (0, 101):
        with pytest.raises(ValueError):
            service._recover_expired_review_claims(batch_size=batch_size)


def test_expire_homeowner_review_invitations_records_expired_ids(
    monkeypatch,
):
    cursor = RecoveryCursor(
        invitations=[
            {"invitation_id": 61},
            {"invitation_id": 62},
        ]
    )
    connection = RecoveryConnection(cursor)

    monkeypatch.setattr(service, "get_connection", lambda: connection)

    result = service.expire_homeowner_review_invitations()

    assert result == {
        "expired_count": 2,
        "expired_invitation_ids": [61, 62],
    }

    assert len(cursor.queries) == 1
    sql, params = cursor.queries[0]

    assert "expires_at <= CURRENT_TIMESTAMP" in sql
    assert "submitted_at IS NULL" in sql
    assert "retry_state NOT IN ('EXPIRED', 'UNKNOWN')" in sql
    assert "claimed_until IS NULL" in sql
    assert "'EXPIRED'" in sql
    assert "FOR UPDATE SKIP LOCKED" in sql
    assert "'EXPIRED'" in sql
    assert params == (100,)


def test_expire_homeowner_review_invitations_rejects_invalid_batch_size():
    for batch_size in (0, 101):
        with pytest.raises(ValueError):
            service.expire_homeowner_review_invitations(
                batch_size=batch_size
            )

