
import json

from app.database.connection import get_connection
from app.services import outbox_publisher


def create_test_event():
    """Insert an isolated outbox event for publisher testing."""
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                INSERT INTO outbox_events (
                    event_key,
                    event_type,
                    payload
                )
                VALUES (
                    'publisher-test-' || gen_random_uuid()::text,
                    'OpportunityExpired',
                    %s::jsonb
                )
                RETURNING id, event_key
                """,
                (
                    json.dumps({
                        "lead_id": "EMG-TEST-LEAD",
                        "expired_opportunity_id": "EMG-TEST-OPPORTUNITY",
                    }),
                ),
            )
            return cursor.fetchone()


def test_publisher_marks_event_processed_after_sns_success(
    monkeypatch,
):
    event = create_test_event()
    captured = {}

    class FakeSNS:
        def publish(self, **kwargs):
            captured.update(kwargs)
            return {"MessageId": "fake-sns-message-123"}

    monkeypatch.setenv(
        "EMG_EVENTS_TOPIC_ARN",
        "arn:aws:sns:us-west-2:123456789012:emg-test",
    )
    monkeypatch.setattr(
        outbox_publisher.boto3,
        "client",
        lambda service: FakeSNS(),
    )

    try:
        result = outbox_publisher.publish_outbox_events()

        assert result["claimed_count"] == 1
        assert result["published_count"] == 1
        assert result["failed_count"] == 0

        assert captured["TopicArn"].endswith(":emg-test")
        assert captured["MessageAttributes"]["event_type"][
            "StringValue"
        ] == "OpportunityExpired"

        message = json.loads(captured["Message"])
        assert message["event_id"] == event["event_key"]
        assert message["payload"]["lead_id"] == "EMG-TEST-LEAD"

        with get_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT processed_at, claimed_until, last_error
                    FROM outbox_events
                    WHERE id = %s
                    """,
                    (event["id"],),
                )
                stored = cursor.fetchone()

        assert stored["processed_at"] is not None
        assert stored["claimed_until"] is None
        assert stored["last_error"] is None

    finally:
        with get_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "DELETE FROM outbox_events WHERE id = %s",
                    (event["id"],),
                )


def test_publisher_releases_event_after_sns_failure(monkeypatch):
    event = create_test_event()

    class FakeSNS:
        def publish(self, **kwargs):
            raise RuntimeError("Simulated SNS failure")

    monkeypatch.setenv(
        "EMG_EVENTS_TOPIC_ARN",
        "arn:aws:sns:us-west-2:123456789012:emg-test",
    )
    monkeypatch.setattr(
        outbox_publisher.boto3,
        "client",
        lambda service: FakeSNS(),
    )

    try:
        result = outbox_publisher.publish_outbox_events(batch_size=1)

        assert result["claimed_count"] == 1
        assert result["published_count"] == 0
        assert result["failed_count"] == 1
        assert result["failed_events"][0]["error_type"] == (
            "RuntimeError"
        )

        with get_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT processed_at, claimed_until, attempts, last_error
                    FROM outbox_events
                    WHERE id = %s
                    """,
                    (event["id"],),
                )
                stored = cursor.fetchone()

        assert stored["processed_at"] is None
        assert stored["claimed_until"] is None
        assert stored["attempts"] == 1
        assert "Simulated SNS failure" in stored["last_error"]

    finally:
        with get_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    "DELETE FROM outbox_events WHERE id = %s",
                    (event["id"],),
                )