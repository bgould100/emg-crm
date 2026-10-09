
import json

import pytest

import app.lambda_handlers.expiration as expiration


def test_scheduler_queues_expiration_job(monkeypatch):
    monkeypatch.setenv(
        "EMG_EXPIRATION_QUEUE_URL",
        "https://example.com/test-queue",
    )

    sent = {}

    class FakeSQS:
        def send_message(self, **kwargs):
            sent.update(kwargs)
            return {"MessageId": "test-message-123"}

    monkeypatch.setattr(
        expiration.boto3,
        "client",
        lambda service: FakeSQS(),
    )

    result = expiration.scheduled_handler({}, None)

    assert result["status"] == "queued"
    assert result["message_id"] == "test-message-123"
    assert sent["QueueUrl"] == "https://example.com/test-queue"

    body = json.loads(sent["MessageBody"])
    assert body["job"] == "expire_overdue_opportunities"
    assert body["batch_size"] == 100


def test_scheduler_fails_without_queue_configuration(monkeypatch):
    monkeypatch.delenv(
        "EMG_EXPIRATION_QUEUE_URL",
        raising=False,
    )

    with pytest.raises(RuntimeError, match="not configured"):
        expiration.scheduled_handler({}, None)


def test_worker_runs_expiration_service(monkeypatch):
    calls = []

    def fake_expire(*, batch_size):
        calls.append(batch_size)
        return {
            "expired_count": 2,
            "expired_opportunity_ids": ["EMGO-1", "EMGO-2"],
            "replacement_count": 2,
            "replacement_opportunities": [],
        }

    monkeypatch.setattr(
        expiration,
        "expire_overdue_opportunities",
        fake_expire,
    )

    event = {
        "Records": [
            {
                "messageId": "message-1",
                "body": json.dumps(
                    {
                        "job": "expire_overdue_opportunities",
                        "batch_size": 25,
                    }
                ),
            }
        ]
    }

    result = expiration.worker_handler(event, None)

    assert calls == [25]
    assert result == {"batchItemFailures": []}


def test_worker_returns_failed_message_for_retry(monkeypatch):
    def fake_expire(*, batch_size):
        raise RuntimeError("Simulated database failure")

    monkeypatch.setattr(
        expiration,
        "expire_overdue_opportunities",
        fake_expire,
    )

    event = {
        "Records": [
            {
                "messageId": "message-fails",
                "body": json.dumps(
                    {"job": "expire_overdue_opportunities"}
                ),
            }
        ]
    }

    result = expiration.worker_handler(event, None)

    assert result == {
        "batchItemFailures": [
            {"itemIdentifier": "message-fails"}
        ]
    }


def test_worker_rejects_unknown_job():
    event = {
        "Records": [
            {
                "messageId": "unknown-job",
                "body": json.dumps({"job": "something_else"}),
            }
        ]
    }

    result = expiration.worker_handler(event, None)

    assert result == {
        "batchItemFailures": [
            {"itemIdentifier": "unknown-job"}
        ]
    }