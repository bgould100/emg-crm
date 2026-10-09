
import json
import os
from typing import Any

import boto3

from app.database.connection import get_connection


LEASE_MINUTES = 5


def publish_outbox_events(batch_size: int = 100) -> dict[str, Any]:
    """
    Publish claimed PostgreSQL outbox events to an SNS topic.

    An event is marked processed only after SNS confirms publication.
    Failed publications remain retryable.
    """

    if not 1 <= batch_size <= 1000:
        raise ValueError("Batch size must be between 1 and 1000.")

    topic_arn = os.getenv("EMG_EVENTS_TOPIC_ARN")
    if not topic_arn:
        raise RuntimeError("EMG_EVENTS_TOPIC_ARN is not configured.")

    sns = boto3.client("sns")
    published: list[str] = []
    failed: list[dict[str, Any]] = []

    # Claim events in a short transaction so other publishers can work
    # on different events concurrently.
    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                WITH selected AS (
                    SELECT id
                    FROM outbox_events
                    WHERE processed_at IS NULL
                      AND (
                          claimed_until IS NULL
                          OR claimed_until <= CURRENT_TIMESTAMP
                      )
                    ORDER BY created_at, id
                    FOR UPDATE SKIP LOCKED
                    LIMIT %s
                )
                UPDATE outbox_events AS event
                SET claimed_until =
                        CURRENT_TIMESTAMP
                        + (%s * INTERVAL '1 minute'),
                    attempts = event.attempts + 1
                FROM selected
                WHERE event.id = selected.id
                RETURNING
                    event.id,
                    event.event_key,
                    event.event_type,
                    event.payload,
                    event.claimed_until
                """,
                (batch_size, LEASE_MINUTES),
            )
            events = cursor.fetchall()

    for event in events:
        event_id = event["id"]
        event_key = event["event_key"]
        claim_timestamp = event["claimed_until"]

        try:
            message = {
                "event_id": event_key,
                "event_type": event["event_type"],
                "payload": event["payload"],
            }

            response = sns.publish(
                TopicArn=topic_arn,
                Message=json.dumps(message, default=str),
                MessageAttributes={
                    "event_type": {
                        "DataType": "String",
                        "StringValue": event["event_type"],
                    }
                },
            )

            # Complete only if this worker still owns the original claim.
            with get_connection() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        UPDATE outbox_events
                        SET processed_at = CURRENT_TIMESTAMP,
                            claimed_until = NULL,
                            last_error = NULL
                        WHERE id = %s
                          AND processed_at IS NULL
                          AND claimed_until = %s
                        """,
                        (event_id, claim_timestamp),
                    )
                    still_owned = cursor.rowcount == 1

            if still_owned:
                published.append(event_key)
            else:
                failed.append({
                    "event_key": event_key,
                    "error_type": "ClaimLost",
                    "sns_message_id": response.get("MessageId"),
                })

        except Exception as error:
            # Release only our own claim so a later attempt can retry.
            with get_connection() as connection:
                with connection.cursor() as cursor:
                    cursor.execute(
                        """
                        UPDATE outbox_events
                        SET claimed_until = NULL,
                            last_error = %s
                        WHERE id = %s
                          AND processed_at IS NULL
                          AND claimed_until = %s
                        """,
                        (
                            f"{type(error).__name__}: {error}"[:2000],
                            event_id,
                            claim_timestamp,
                        ),
                    )

            failed.append({
                "event_key": event_key,
                "error_type": type(error).__name__,
            })

    return {
        "claimed_count": len(events),
        "published_count": len(published),
        "published_event_keys": published,
        "failed_count": len(failed),
        "failed_events": failed,
    }