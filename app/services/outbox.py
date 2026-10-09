
from typing import Any

from app.services.matching import route_opportunities_to_fill_slots


def process_outbox_event(event: dict[str, Any]) -> dict[str, Any]:
    """
    Process one event delivered downstream from SNS through SQS.

    This function does not claim or update PostgreSQL outbox rows.
    The SNS publisher owns outbox delivery; SQS/Lambda owns retries
    for downstream event processing.
    """

    if not isinstance(event, dict):
        raise ValueError("Event must be a JSON object.")

    event_type = event.get("event_type")
    if event_type != "OpportunityExpired":
        raise ValueError(f"Unsupported event type: {event_type}")

    payload = event.get("payload")
    if not isinstance(payload, dict):
        raise ValueError("Event payload must be an object.")

    lead_id = payload.get("lead_id")
    if not isinstance(lead_id, str) or not lead_id.strip():
        raise ValueError("Event payload is missing a valid lead_id.")

    expired_opportunity_id = payload.get("expired_opportunity_id")
    if (
        not isinstance(expired_opportunity_id, str)
        or not expired_opportunity_id.strip()
    ):
        raise ValueError(
            "Event payload is missing a valid expired_opportunity_id."
        )

    # The matching service enforces open-slot capacity and prevents
    # inviting the same contractor to the same lead more than once.
    replacements = route_opportunities_to_fill_slots(lead_id)

    return {
        "status": "processed",
        "event_id": event.get("event_id"),
        "event_type": event_type,
        "lead_id": lead_id,
        "expired_opportunity_id": expired_opportunity_id,
        "replacement_count": len(replacements),
        "replacement_opportunities": replacements,
    }