
import json

from app.lambda_handlers import outbox_events


def make_emg_event():
    return {
        "event_id": "opportunity-expired:EMGO-OLD-001:refill",
        "event_type": "OpportunityExpired",
        "payload": {
            "lead_id": "EMG-TEST-001",
            "expired_opportunity_id": "EMGO-OLD-001",
        },
    }


def make_sqs_record(message_id, body):
    return {
        "messageId": message_id,
        "body": json.dumps(body),
    }


def test_processes_sns_wrapped_sqs_message(monkeypatch):
    emg_event = make_emg_event()
    processed_events = []

    def fake_processor(event):
        processed_events.append(event)
        return {
            "status": "processed",
            "event_id": event["event_id"],
            "replacement_count": 1,
        }

    monkeypatch.setattr(
        outbox_events,
        "process_outbox_event",
        fake_processor,
    )

    sns_envelope = {
        "Type": "Notification",
        "MessageId": "sns-message-001",
        "Message": json.dumps(emg_event),
    }

    result = outbox_events.worker_handler(
        {
            "Records": [
                make_sqs_record("sqs-message-001", sns_envelope)
            ]
        },
        None,
    )

    assert result == {"batchItemFailures": []}
    assert processed_events == [emg_event]


def test_processes_direct_sqs_event(monkeypatch):
    emg_event = make_emg_event()
    processed_events = []

    def fake_processor(event):
        processed_events.append(event)
        return {
            "status": "processed",
            "event_id": event["event_id"],
            "replacement_count": 0,
        }

    monkeypatch.setattr(
        outbox_events,
        "process_outbox_event",
        fake_processor,
    )

    result = outbox_events.worker_handler(
        {
            "Records": [
                make_sqs_record("sqs-message-002", emg_event)
            ]
        },
        None,
    )

    assert result == {"batchItemFailures": []}
    assert processed_events == [emg_event]


def test_reports_failed_message_for_retry(monkeypatch):
    def fake_processor(event):
        raise RuntimeError("Simulated routing failure")

    monkeypatch.setattr(
        outbox_events,
        "process_outbox_event",
        fake_processor,
    )

    result = outbox_events.worker_handler(
        {
            "Records": [
                make_sqs_record("sqs-message-003", make_emg_event())
            ]
        },
        None,
    )

    assert result == {
        "batchItemFailures": [
            {"itemIdentifier": "sqs-message-003"}
        ]
    }


def test_successful_message_is_not_retried_with_failed_message(
    monkeypatch,
):
    def fake_processor(event):
        if event["payload"]["lead_id"] == "EMG-FAIL":
            raise RuntimeError("Simulated failure")

        return {
            "status": "processed",
            "event_id": event["event_id"],
            "replacement_count": 0,
        }

    monkeypatch.setattr(
        outbox_events,
        "process_outbox_event",
        fake_processor,
    )

    successful_event = make_emg_event()
    failed_event = make_emg_event()
    failed_event["payload"]["lead_id"] = "EMG-FAIL"

    result = outbox_events.worker_handler(
        {
            "Records": [
                make_sqs_record("sqs-success", successful_event),
                make_sqs_record("sqs-failure", failed_event),
            ]
        },
        None,
    )

    assert result == {
        "batchItemFailures": [
            {"itemIdentifier": "sqs-failure"}
        ]
    }


def test_malformed_sns_message_is_reported_as_failure():
    sns_envelope = {
        "Type": "Notification",
        "Message": "{not valid JSON",
    }

    result = outbox_events.worker_handler(
        {
            "Records": [
                make_sqs_record("sqs-malformed", sns_envelope)
            ]
        },
        None,
    )

    assert result == {
        "batchItemFailures": [
            {"itemIdentifier": "sqs-malformed"}
        ]
    }


def test_rejects_unsupported_event_type_for_retry():
    event = make_emg_event()
    event["event_type"] = "UnknownEvent"

    result = outbox_events.worker_handler(
        {
            "Records": [
                make_sqs_record("sqs-unsupported", event)
            ]
        },
        None,
    )

    assert result == {
        "batchItemFailures": [
            {"itemIdentifier": "sqs-unsupported"}
        ]
    }