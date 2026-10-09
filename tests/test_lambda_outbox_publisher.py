from unittest.mock import Mock

import pytest

from app.lambda_handlers import outbox_publisher


def test_scheduled_handler_uses_default_batch_size(monkeypatch):
    fake_publisher = Mock(
        return_value={
            "claimed_count": 3,
            "published_count": 3,
            "published_event_keys": ["event-1", "event-2", "event-3"],
            "failed_count": 0,
            "failed_events": [],
        }
    )

    monkeypatch.setattr(
        outbox_publisher,
        "publish_outbox_events",
        fake_publisher,
    )

    result = outbox_publisher.scheduled_handler({}, None)

    fake_publisher.assert_called_once_with(batch_size=100)
    assert result["status"] == "completed"
    assert result["published_count"] == 3


def test_scheduled_handler_accepts_custom_batch_size(monkeypatch):
    fake_publisher = Mock(
        return_value={
            "claimed_count": 5,
            "published_count": 5,
            "published_event_keys": [],
            "failed_count": 0,
            "failed_events": [],
        }
    )

    monkeypatch.setattr(
        outbox_publisher,
        "publish_outbox_events",
        fake_publisher,
    )

    result = outbox_publisher.scheduled_handler(
        {"batch_size": 5},
        None,
    )

    fake_publisher.assert_called_once_with(batch_size=5)
    assert result["status"] == "completed"


def test_scheduled_handler_reports_publication_failures(monkeypatch):
    monkeypatch.setattr(
        outbox_publisher,
        "publish_outbox_events",
        Mock(
            return_value={
                "claimed_count": 2,
                "published_count": 1,
                "published_event_keys": ["event-1"],
                "failed_count": 1,
                "failed_events": [
                    {"event_key": "event-2", "error_type": "RuntimeError"}
                ],
            }
        ),
    )

    result = outbox_publisher.scheduled_handler({}, None)

    assert result["status"] == "completed_with_failures"
    assert result["failed_count"] == 1


@pytest.mark.parametrize(
    "batch_size",
    [0, -1, 1001, 1.5, True, "10", None],
)
def test_scheduled_handler_rejects_invalid_batch_size(batch_size):
    with pytest.raises(ValueError, match="batch_size"):
        outbox_publisher.scheduled_handler(
            {"batch_size": batch_size},
            None,
        )