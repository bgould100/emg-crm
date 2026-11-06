
"""Deliver homeowner review invitations through Amazon SES."""

import json
import os
import random
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote

import boto3

from app.database.connection import get_connection
from app.services.review_tokens import (
    generate_review_token,
    hash_review_token,
)


LEASE_MINUTES = 5
MAX_FAILURE_STREAK = 5
MAX_BATCH_SIZE = 100


def _email_configuration() -> tuple[str, str, Any]:
    sender = os.getenv("EMG_REVIEW_EMAIL_FROM", "").strip()
    base_url = os.getenv("EMG_REVIEW_BASE_URL", "").strip()
    region = (
        os.getenv("EMG_SES_REGION")
        or os.getenv("AWS_REGION")
        or os.getenv("AWS_DEFAULT_REGION")
    )

    if not sender:
        raise RuntimeError("EMG_REVIEW_EMAIL_FROM is not configured.")

    if not base_url.startswith("https://"):
        raise RuntimeError(
            "EMG_REVIEW_BASE_URL must be an HTTPS URL."
        )

    client_options = {"region_name": region} if region else {}
    ses = boto3.client("ses", **client_options)

    return sender, base_url, ses


def _claim_due_invitations(batch_size: int) -> list[dict[str, Any]]:
    """Claim due invitations and record the beginning of each attempt."""

    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                WITH selected AS (
                    SELECT id
                    FROM homeowner_review_invitations
                    WHERE retry_state = 'ACTIVE'
                      AND delivery_status IN ('PENDING', 'FAILED')
                      AND submitted_at IS NULL
                      AND customer_email IS NOT NULL
                      AND BTRIM(customer_email) <> ''
                      AND expires_at > CURRENT_TIMESTAMP
                      AND next_attempt_at <= CURRENT_TIMESTAMP
                      AND (
                          claimed_until IS NULL
                          OR claimed_until <= CURRENT_TIMESTAMP
                      )
                    ORDER BY next_attempt_at, created_at, id
                    FOR UPDATE SKIP LOCKED
                    LIMIT %s
                )
                UPDATE homeowner_review_invitations AS invitation
                SET claimed_until =
                        CURRENT_TIMESTAMP + (%s * INTERVAL '1 minute'),
                    attempts = invitation.attempts + 1,
                    last_attempt_at = CURRENT_TIMESTAMP
                FROM selected
                WHERE invitation.id = selected.id
                RETURNING
                    invitation.id,
                    invitation.customer_email,
                    invitation.attempts,
                    invitation.failure_streak,
                    invitation.claimed_until
                """,
                (batch_size, LEASE_MINUTES),
            )

            invitations = cursor.fetchall()

            for invitation in invitations:
                token = generate_review_token(invitation["id"])
                token_hash = hash_review_token(token)

                cursor.execute(
                    """
                    UPDATE homeowner_review_invitations
                    SET token_hash = %s
                    WHERE id = %s
                      AND claimed_until = %s
                    """,
                    (
                        token_hash,
                        invitation["id"],
                        invitation["claimed_until"],
                    ),
                )

                cursor.execute(
                    """
                    INSERT INTO homeowner_review_delivery_events (
                        invitation_id,
                        event_type,
                        attempt_number,
                        failure_streak,
                        details
                    )
                    VALUES (%s, 'ATTEMPT_STARTED', %s, %s, %s::jsonb)
                    """,
                    (
                        invitation["id"],
                        invitation["attempts"],
                        invitation["failure_streak"],
                        json.dumps({}),
                    ),
                )

                # Keep the plaintext token in worker memory only.
                invitation["token"] = token

    return invitations


def _send_invitation(
    ses: Any,
    sender: str,
    base_url: str,
    email: str,
    token: str,
) -> str:
    """Send one review invitation and return the SES message ID."""

    separator = "&" if "?" in base_url else "?"
    review_url = (
        f"{base_url}{separator}token={quote(token, safe='')}"
    )

    response = ses.send_email(
        Source=sender,
        Destination={"ToAddresses": [email]},
        Message={
            "Subject": {
                "Charset": "UTF-8",
                "Data": "How was your recent project?",
            },
            "Body": {
                "Text": {
                    "Charset": "UTF-8",
                    "Data": (
                        "Your recent project has been marked complete. "
                        "We would appreciate your feedback.\n\n"
                        f"Submit your review here:\n{review_url}\n\n"
                        "This invitation expires in 30 days."
                    ),
                },
                "Html": {
                    "Charset": "UTF-8",
                    "Data": (
                        "<p>Your recent project has been marked complete. "
                        "We would appreciate your feedback.</p>"
                        f'<p><a href="{review_url}">'
                        "Submit your review</a></p>"
                        "<p>This invitation expires in 30 days.</p>"
                    ),
                },
            },
        },
    )

    message_id = response.get("MessageId")
    if not message_id:
        raise RuntimeError("SES returned no message ID.")

    return message_id


def _record_success(
    invitation: dict[str, Any],
    message_id: str,
) -> bool:
    """Record provider acceptance only if this worker owns the claim."""

    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE homeowner_review_invitations
                SET delivery_status = 'SENT',
                    retry_state = 'COMPLETE',
                    email_sent_at = CURRENT_TIMESTAMP,
                    provider_message_id = %s,
                    last_error = NULL,
                    next_attempt_at = NULL,
                    claimed_until = NULL,
                    failure_streak = 0
                WHERE id = %s
                  AND claimed_until = %s
                  AND retry_state = 'ACTIVE'
                  AND submitted_at IS NULL
                  AND delivery_status IN ('PENDING', 'FAILED')
                """,
                (
                    message_id,
                    invitation["id"],
                    invitation["claimed_until"],
                ),
            )

            if cursor.rowcount != 1:
                return False

            cursor.execute(
                """
                INSERT INTO homeowner_review_delivery_events (
                    invitation_id,
                    event_type,
                    attempt_number,
                    failure_streak,
                    details
                )
                VALUES
                    (%s, 'ATTEMPT_SUCCEEDED', %s, 0, %s::jsonb),
                    (%s, 'COMPLETED', %s, 0, %s::jsonb)
                """,
                (
                    invitation["id"],
                    invitation["attempts"],
                    json.dumps({"provider_message_id": message_id}),
                    invitation["id"],
                    invitation["attempts"],
                    json.dumps({"reason": "SES accepted the email"}),
                ),
            )

    return True


def _record_failure(
    invitation: dict[str, Any],
    error: Exception,
) -> dict[str, Any]:
    """Schedule a retry or pause after five consecutive failures."""

    new_failure_streak = invitation["failure_streak"] + 1
    paused = new_failure_streak >= MAX_FAILURE_STREAK

    delay_seconds = min(
        3600,
        60 * (2 ** (new_failure_streak - 1)),
    )
    delay_seconds *= random.uniform(1.0, 1.2)

    error_message = f"{type(error).__name__}: {error}"[:2000]

    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE homeowner_review_invitations
                SET delivery_status = 'FAILED',
                    failure_streak = failure_streak + 1,
                    retry_state = CASE
                        WHEN failure_streak + 1 >= %s
                            THEN 'PAUSED'
                        ELSE 'ACTIVE'
                    END,
                    paused_at = CASE
                        WHEN failure_streak + 1 >= %s
                            THEN CURRENT_TIMESTAMP
                        ELSE paused_at
                    END,
                    next_attempt_at = CASE
                        WHEN failure_streak + 1 >= %s
                            THEN NULL
                        ELSE CURRENT_TIMESTAMP
                            + (%s * INTERVAL '1 second')
                    END,
                    claimed_until = NULL,
                    last_error = %s
                WHERE id = %s
                  AND claimed_until = %s
                  AND retry_state = 'ACTIVE'
                  AND submitted_at IS NULL
                RETURNING failure_streak, retry_state
                """,
                (
                    MAX_FAILURE_STREAK,
                    MAX_FAILURE_STREAK,
                    MAX_FAILURE_STREAK,
                    delay_seconds,
                    error_message,
                    invitation["id"],
                    invitation["claimed_until"],
                ),
            )

            result = cursor.fetchone()

            if result is None:
                return {"recorded": False, "paused": False}

            cursor.execute(
                """
                INSERT INTO homeowner_review_delivery_events (
                    invitation_id,
                    event_type,
                    attempt_number,
                    failure_streak,
                    details
                )
                VALUES (%s, 'ATTEMPT_FAILED', %s, %s, %s::jsonb)
                """,
                (
                    invitation["id"],
                    invitation["attempts"],
                    result["failure_streak"],
                    json.dumps({
                        "error_type": type(error).__name__,
                        "error": error_message,
                    }),
                ),
            )

            if result["retry_state"] == "PAUSED":
                cursor.execute(
                    """
                    INSERT INTO homeowner_review_delivery_events (
                        invitation_id,
                        event_type,
                        attempt_number,
                        failure_streak,
                        details
                    )
                    VALUES (%s, 'PAUSED', %s, %s, %s::jsonb)
                    """,
                    (
                        invitation["id"],
                        invitation["attempts"],
                        result["failure_streak"],
                        json.dumps({
                            "reason": "Maximum consecutive failures reached"
                        }),
                    ),
                )

            return {
                "recorded": True,
                "paused": result["retry_state"] == "PAUSED",
                "failure_streak": result["failure_streak"],
            }


def deliver_homeowner_review_invitations(
    batch_size: int = 25,
) -> dict[str, Any]:
    """Deliver due homeowner review invitations through Amazon SES."""

    if not 1 <= batch_size <= MAX_BATCH_SIZE:
        raise ValueError(
            f"Batch size must be between 1 and {MAX_BATCH_SIZE}."
        )

    sender, base_url, ses = _email_configuration()
    invitations = _claim_due_invitations(batch_size)

    sent: list[int] = []
    failed: list[dict[str, Any]] = []
    paused: list[int] = []
    claim_lost: list[int] = []

    for invitation in invitations:
        try:
            message_id = _send_invitation(
                ses,
                sender,
                base_url,
                invitation["customer_email"],
                invitation["token"],
            )
        except Exception as error:
            result = _record_failure(invitation, error)

            if result["recorded"]:
                failed.append({
                    "invitation_id": invitation["id"],
                    "failure_streak": result["failure_streak"],
                })
                if result["paused"]:
                    paused.append(invitation["id"])
            else:
                claim_lost.append(invitation["id"])

            continue

        if _record_success(invitation, message_id):
            sent.append(invitation["id"])
        else:
            claim_lost.append(invitation["id"])

    return {
        "claimed_count": len(invitations),
        "sent_count": len(sent),
        "sent_invitation_ids": sent,
        "failed_count": len(failed),
        "failed_invitations": failed,
        "paused_invitation_ids": paused,
        "claim_lost_invitation_ids": claim_lost,
    }

class ReviewInvitationNotFoundError(Exception):
    """Raised when an administrator references a nonexistent invitation."""


class ReviewInvitationConflictError(Exception):
    """Raised when an invitation cannot accept the requested action."""


def _validate_admin_reason(reason: str) -> str:
    reason = reason.strip()

    if len(reason) < 3:
        raise ValueError("A reason of at least 3 characters is required.")

    if len(reason) > 1000:
        raise ValueError("The reason cannot exceed 1000 characters.")

    return reason


def resume_homeowner_review_invitation(
    invitation_id: int,
    admin_id: int,
    reason: str,
) -> dict[str, Any]:
    """Resume a paused invitation and record the administrator action."""

    reason = _validate_admin_reason(reason)

    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT
                    id,
                    retry_state,
                    attempts,
                    failure_streak,
                    expires_at,
                    submitted_at
                FROM homeowner_review_invitations
                WHERE id = %s
                FOR UPDATE
                """,
                (invitation_id,),
            )
            invitation = cursor.fetchone()

            if invitation is None:
                raise ReviewInvitationNotFoundError(
                    "Review invitation not found."
                )

            if invitation["submitted_at"] is not None:
                raise ReviewInvitationConflictError(
                    "This invitation has already been submitted."
                )

            if invitation["expires_at"] <= datetime.now(timezone.utc):
                raise ReviewInvitationConflictError(
                    "This invitation has expired."
                )

            if invitation["retry_state"] != "PAUSED":
                raise ReviewInvitationConflictError(
                    "Only paused invitations can be resumed."
                )

            cursor.execute(
                """
                UPDATE homeowner_review_invitations
                SET retry_state = 'ACTIVE',
                    failure_streak = 0,
                    next_attempt_at = CURRENT_TIMESTAMP,
                    claimed_until = NULL,
                    paused_at = NULL,
                    retry_control_admin_id = %s,
                    retry_control_reason = %s
                WHERE id = %s
                RETURNING attempts, failure_streak
                """,
                (admin_id, reason, invitation_id),
            )
            updated = cursor.fetchone()

            cursor.execute(
                """
                INSERT INTO homeowner_review_delivery_events (
                    invitation_id,
                    event_type,
                    attempt_number,
                    failure_streak,
                    admin_user_id,
                    details
                )
                VALUES (%s, 'RESUMED', %s, %s, %s, %s::jsonb)
                """,
                (
                    invitation_id,
                    updated["attempts"],
                    updated["failure_streak"],
                    admin_id,
                    json.dumps({"reason": reason}),
                ),
            )

    return {
        "invitation_id": invitation_id,
        "retry_state": "ACTIVE",
        "message": "Review invitation retries have been resumed.",
    }


def stop_homeowner_review_invitation(
    invitation_id: int,
    admin_id: int,
    reason: str,
) -> dict[str, Any]:
    """Stop automatic retries and record the administrator action."""

    reason = _validate_admin_reason(reason)

    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT
                    id,
                    retry_state,
                    attempts,
                    failure_streak,
                    expires_at,
                    submitted_at
                FROM homeowner_review_invitations
                WHERE id = %s
                FOR UPDATE
                """,
                (invitation_id,),
            )
            invitation = cursor.fetchone()

            if invitation is None:
                raise ReviewInvitationNotFoundError(
                    "Review invitation not found."
                )

            if invitation["submitted_at"] is not None:
                raise ReviewInvitationConflictError(
                    "This invitation has already been submitted."
                )

            if invitation["expires_at"] <= datetime.now(timezone.utc):
                raise ReviewInvitationConflictError(
                    "This invitation has expired."
                )

            if invitation["retry_state"] not in ("ACTIVE", "PAUSED"):
                raise ReviewInvitationConflictError(
                    "Only active or paused invitations can be stopped."
                )

            cursor.execute(
                """
                UPDATE homeowner_review_invitations
                SET retry_state = 'STOPPED',
                    stopped_at = CURRENT_TIMESTAMP,
                    paused_at = NULL,
                    next_attempt_at = NULL,
                    claimed_until = NULL,
                    retry_control_admin_id = %s,
                    retry_control_reason = %s
                WHERE id = %s
                RETURNING attempts, failure_streak
                """,
                (admin_id, reason, invitation_id),
            )
            updated = cursor.fetchone()

            cursor.execute(
                """
                INSERT INTO homeowner_review_delivery_events (
                    invitation_id,
                    event_type,
                    attempt_number,
                    failure_streak,
                    admin_user_id,
                    details
                )
                VALUES (%s, 'STOPPED', %s, %s, %s, %s::jsonb)
                """,
                (
                    invitation_id,
                    updated["attempts"],
                    updated["failure_streak"],
                    admin_id,
                    json.dumps({"reason": reason}),
                ),
            )

    return {
        "invitation_id": invitation_id,
        "retry_state": "STOPPED",
        "message": "Automatic review invitation retries have been stopped.",
    }


def expire_homeowner_review_invitations(
    batch_size: int = 100,
) -> dict[str, Any]:
    """Expire overdue, unsubmitted invitations and record audit events."""

    if not 1 <= batch_size <= MAX_BATCH_SIZE:
        raise ValueError(
            f"Batch size must be between 1 and {MAX_BATCH_SIZE}."
        )

    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                WITH selected AS (
                    SELECT id
                    FROM homeowner_review_invitations
                    WHERE expires_at <= CURRENT_TIMESTAMP
                      AND submitted_at IS NULL
                      AND retry_state <> 'EXPIRED'
                    ORDER BY expires_at, id
                    FOR UPDATE SKIP LOCKED
                    LIMIT %s
                ),
                expired AS (
                    UPDATE homeowner_review_invitations AS invitation
                    SET retry_state = 'EXPIRED',
                        next_attempt_at = NULL,
                        claimed_until = NULL,
                        paused_at = NULL,
                        stopped_at = NULL
                    FROM selected
                    WHERE invitation.id = selected.id
                    RETURNING
                        invitation.id,
                        invitation.attempts,
                        invitation.failure_streak
                )
                INSERT INTO homeowner_review_delivery_events (
                    invitation_id,
                    event_type,
                    attempt_number,
                    failure_streak,
                    details
                )
                SELECT
                    id,
                    'EXPIRED',
                    NULLIF(attempts, 0),
                    failure_streak,
                    jsonb_build_object(
                        'reason',
                        'The 30-day review invitation deadline passed'
                    )
                FROM expired
                RETURNING invitation_id
                """,
                (batch_size,),
            )

            expired_ids = [
                row["invitation_id"]
                for row in cursor.fetchall()
            ]

    return {
        "expired_count": len(expired_ids),
        "expired_invitation_ids": expired_ids,
    }