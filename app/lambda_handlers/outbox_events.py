
import json

from app.services.outbox import process_outbox_event


def worker_handler(event, context):
    """
    Process events delivered by an SQS queue subscribed to SNS.

    Failed messages are returned to Lambda for retry through SQS.
    The event source mapping must have partial batch responses enabled.
    """

    batch_item_failures = []

    for record in event.get("Records", []):
        message_id = record.get("messageId", "unknown")

        try:
            body = json.loads(record["body"])

            if not isinstance(body, dict):
                raise ValueError("SQS message body must be a JSON object.")

            # SNS-to-SQS subscriptions wrap the published message
            # inside the SNS notification's "Message" field.
            if body.get("Type") == "Notification":
                message = body.get("Message")

                if not isinstance(message, str):
                    raise ValueError(
                        "SNS notification is missing its Message string."
                    )

                emg_event = json.loads(message)
            else:
                # Also accept a direct event body for testing and
                # alternative queue integrations.
                emg_event = body

            if not isinstance(emg_event, dict):
                raise ValueError("EMG event must be a JSON object.")

            result = process_outbox_event(emg_event)

            print(
                json.dumps(
                    {
                        "status": "completed",
                        "sqs_message_id": message_id,
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
                        "sqs_message_id": message_id,
                        "error_type": type(error).__name__,
                    }
                )
            )
            batch_item_failures.append(
                {"itemIdentifier": message_id}
            )

    return {"batchItemFailures": batch_item_failures}