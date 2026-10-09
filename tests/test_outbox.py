
import pytest

from app.services import outbox


def test_processes_opportunity_expired_event(monkeypatch):
    expected_replacements = [
        {
            "opportunity_id": "EMGO-TEST-001",
            "contractor_id": "EMGC-TEST-001",
        }
    ]
    routing_calls = []

    def fake_route(lead_id):
        routing_calls.append(lead_id)
        return expected_replacements

    monkeypatch.setattr(
        outbox,
        "route_opportunities_to_fill_slots",
        fake_route,
    )

    event = {
        "event_id": "opportunity-expired:EMGO-OLD-001:refill",
        "event_type": "OpportunityExpired",
        "payload": {
            "lead_id": "EMG-TEST-001",
            "expired_opportunity_id": "EMGO-OLD-001",
        },
    }

    result = outbox.process_outbox_event(event)

    assert result["status"] == "processed"
    assert result["event_id"] == event["event_id"]
    assert result["event_type"] == "OpportunityExpired"
    assert result["lead_id"] == "EMG-TEST-001"
    assert result["expired_opportunity_id"] == "EMGO-OLD-001"
    assert result["replacement_count"] == 1
    assert result["replacement_opportunities"] == expected_replacements
    assert routing_calls == ["EMG-TEST-001"]


def test_processes_event_when_no_replacement_is_available(monkeypatch):
    monkeypatch.setattr(
        outbox,
        "route_opportunities_to_fill_slots",
        lambda lead_id: [],
    )

    event = {
        "event_id": "opportunity-expired:EMGO-OLD-002:refill",
        "event_type": "OpportunityExpired",
        "payload": {
            "lead_id": "EMG-TEST-002",
            "expired_opportunity_id": "EMGO-OLD-002",
        },
    }

    result = outbox.process_outbox_event(event)

    assert result["status"] == "processed"
    assert result["replacement_count"] == 0
    assert result["replacement_opportunities"] == []


@pytest.mark.parametrize(
    "event",
    [
        None,
        [],
        "not an event",
    ],
)
def test_rejects_non_object_events(event):
    with pytest.raises(ValueError, match="Event must be a JSON object"):
        outbox.process_outbox_event(event)


def test_rejects_unsupported_event_type():
    event = {
        "event_id": "test-event",
        "event_type": "UnknownEvent",
        "payload": {
            "lead_id": "EMG-TEST-003",
            "expired_opportunity_id": "EMGO-OLD-003",
        },
    }

    with pytest.raises(ValueError, match="Unsupported event type"):
        outbox.process_outbox_event(event)


@pytest.mark.parametrize(
    "payload",
    [
        None,
        [],
        {},
        {"expired_opportunity_id": "EMGO-OLD-004"},
        {
            "lead_id": " ",
            "expired_opportunity_id": "EMGO-OLD-004",
        },
        {
            "lead_id": "EMG-TEST-004",
            "expired_opportunity_id": "",
        },
    ],
)
def test_rejects_invalid_event_payload(payload):
    event = {
        "event_id": "test-event",
        "event_type": "OpportunityExpired",
        "payload": payload,
    }

    with pytest.raises(ValueError):
        outbox.process_outbox_event(event)