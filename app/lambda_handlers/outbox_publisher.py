import json

from app.services.outbox_publisher import publish_outbox_events


DEFAULT_BATCH_SIZE = 100


def scheduled_handler(event, context):
    """
    Publish pending PostgreSQL outbox events to SNS.

    Intended to be invoked periodically by Amazon EventBridge Scheduler.
    """

    event = event or {}

    batch_size = event.get("batch_size", DEFAULT_BATCH_SIZE)

    if (
        not isinstance(batch_size, int)
        or isinstance(batch_size, bool)
        or not 1 <= batch_size <= 1000
    ):
        raise ValueError(
            "batch_size must be an integer from 1 to 1000."
        )

    result = publish_outbox_events(batch_size=batch_size)

    response = {
        "status": (
            "completed"
            if result["failed_count"] == 0
            else "completed_with_failures"
        ),
        **result,
    }

    print(json.dumps(response, default=str))

    return response
