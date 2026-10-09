
import json
import os

import boto3

from app.services.expiration import expire_overdue_opportunities


DEFAULT_BATCH_SIZE = 100
EXPIRATION_JOB = "expire_overdue_opportunities"


def scheduled_handler(event, context):
    """Send an expiration job to SQS."""

    queue_url = os.getenv("EMG_EXPIRATION_QUEUE_URL")

    if not queue_url:
        raise RuntimeError(
            "EMG_EXPIRATION_QUEUE_URL is not configured."
        )

    sqs = boto3.client("sqs")

    response = sqs.send_message(
        QueueUrl=queue_url,
        MessageBody=json.dumps(
            {
                "job": EXPIRATION_JOB,
                "batch_size": DEFAULT_BATCH_SIZE,
            }
        ),
    )

    result = {
        "status": "queued",
        "message_id": response["MessageId"],
        "job": EXPIRATION_JOB,
    }

    print(json.dumps(result))
    return result


def worker_handler(event, context):
    """Process expiration jobs delivered through an SQS event."""

    batch_item_failures = []

    for record in event.get("Records", []):
        message_id = record.get("messageId", "unknown")

        try:
            body = json.loads(record["body"])

            if body.get("job") != EXPIRATION_JOB:
                raise ValueError("Unsupported SQS job type.")

            batch_size = body.get(
                "batch_size",
                DEFAULT_BATCH_SIZE,
            )

            if (
                not isinstance(batch_size, int)
                or isinstance(batch_size, bool)
                or not 1 <= batch_size <= 1000
            ):
                raise ValueError(
                    "batch_size must be an integer from 1 to 1000."
                )

            result = expire_overdue_opportunities(
                batch_size=batch_size
            )

            print(
                json.dumps(
                    {
                        "status": "completed",
                        "message_id": message_id,
                        **result,
                    },
                    default=str,
                )
            )

        except Exception as error:
            print(
                json.dumps(
                    {
                        "status": "failed",
                        "message_id": message_id,
                        "error_type": type(error).__name__,
                    }
                )
            )

            # SQS/Lambda can retry this message.
            batch_item_failures.append(
                {"itemIdentifier": message_id}
            )

    return {"batchItemFailures": batch_item_failures}