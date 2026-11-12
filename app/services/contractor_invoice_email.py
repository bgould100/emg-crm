
"""Send contractor invoice emails through Amazon SES v2."""

import os
from typing import Any

import boto3

class InvoiceEmailPreparationError(RuntimeError):
    """Raised when email preparation fails before SES submission."""

def _email_configuration() -> tuple[str, str, Any]:
    """Load sender configuration and initialize the SES v2 client."""

    sender_email = os.getenv(
        "EMG_SES_FROM_EMAIL",
        "",
    ).strip()

    sender_name = os.getenv(
        "EMG_SES_FROM_NAME",
        "EliteMatch Group",
    ).strip()

    region = (
        os.getenv("EMG_SES_REGION")
        or os.getenv("AWS_REGION")
        or os.getenv("AWS_DEFAULT_REGION")
    )

    if not sender_email:
        raise RuntimeError(
            "EMG_SES_FROM_EMAIL is not configured."
        )

    client_options = {"region_name": region} if region else {}

    ses = boto3.client("sesv2", **client_options)

    return sender_email, sender_name, ses


def send_contractor_invoice_email(
    recipient_email: str,
    invoice_number: str,
    billing_period_start: str,
    billing_period_end: str,
    total_amount: str,
    pdf_bytes: bytes,
) -> str:
    """Send one contractor invoice and return the SES message ID."""

    recipient_email = recipient_email.strip()

    if not recipient_email:
        raise ValueError("A recipient email address is required.")

    if not pdf_bytes:
        raise ValueError("The invoice PDF cannot be empty.")

    try:
        sender_email, sender_name, ses = _email_configuration()
    except Exception as exc:
        raise InvoiceEmailPreparationError(
            "Unable to initialize contractor invoice email configuration."
        ) from exc

    subject = f"EMG Contractor Invoice {invoice_number}"

    body_text = (
        "Hello,\n\n"
        "Your contractor invoice is attached.\n\n"
        f"Invoice: {invoice_number}\n"
        f"Billing period: {billing_period_start} "
        f"through {billing_period_end}\n"
        f"Total: ${total_amount}\n\n"
        "Please review the attached invoice for payment details.\n\n"
        "Thank you,\n"
        "EliteMatch Group"
    )

    reply_to_email = os.getenv(
        "EMG_SES_REPLY_TO_EMAIL",
        "",
    ).strip()

    response = ses.send_email(
        FromEmailAddress=(
            f"{sender_name} <{sender_email}>"
            if sender_name
            else sender_email
        ),
        Destination={
            "ToAddresses": [recipient_email],
        },
        ReplyToAddresses=[reply_to_email] if reply_to_email else [],
        Content={
            "Simple": {
                "Subject": {
                    "Data": subject,
                    "Charset": "UTF-8",
                },
                "Body": {
                    "Text": {
                        "Data": body_text,
                        "Charset": "UTF-8",
                    },
                },
                "Attachments": [
                    {
                        "RawContent": pdf_bytes,
                        "FileName": f"{invoice_number}.pdf",
                        "ContentType": "application/pdf",
                        "ContentDisposition": "ATTACHMENT",
                        "ContentTransferEncoding": "BASE64",
                    },
                ],
            },
        },
    )

    message_id = response.get("MessageId")

    if not message_id:
        raise RuntimeError("SES returned no message ID.")

    return message_id
