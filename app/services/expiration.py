
import json
from typing import Any

from app.database.connection import get_connection


def expire_overdue_opportunities(
    batch_size: int = 100,
) -> dict[str, Any]:
    """Expire overdue, unanswered opportunities and refill open bid slots."""

    if batch_size < 1:
        raise ValueError("Batch size must be at least 1.")

    expired_ids: list[str] = []
    outbox_event_count = 0

    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT
                    o.id AS opportunity_pk,
                    o.lead_id AS lead_pk,
                    l.lead_id AS public_lead_id
                FROM opportunities o
                JOIN leads l ON l.id = o.lead_id
                WHERE o.status = 'PENDING'
                  AND o.expiration_paused = FALSE
                  AND o.expires_at <= CURRENT_TIMESTAMP
                ORDER BY o.expires_at
                LIMIT %s
                """,
                (batch_size,),
            )
            candidates = cursor.fetchall()

            for candidate in candidates:
                # Lock the lead first, consistent with the matching service.
                cursor.execute(
                    """
                    SELECT id
                    FROM leads
                    WHERE id = %s
                    FOR UPDATE
                    """,
                    (candidate["lead_pk"],),
                )

                if cursor.fetchone() is None:
                    continue

                # Recheck the opportunity while holding its row lock.
                cursor.execute(
                    """
                    SELECT
                        id,
                        contractor_id,
                        opportunity_id,
                        status,
                        expiration_paused,
                        expires_at
                    FROM opportunities
                    WHERE id = %s
                    FOR UPDATE
                    """,
                    (candidate["opportunity_pk"],),
                )
                opportunity = cursor.fetchone()

                if opportunity is None:
                    continue

                if opportunity["status"] != "PENDING":
                    continue

                if opportunity["expiration_paused"]:
                    continue

                cursor.execute(
                    """
                    SELECT %s <= CURRENT_TIMESTAMP AS is_overdue
                    """,
                    (opportunity["expires_at"],),
                )

                if not cursor.fetchone()["is_overdue"]:
                    continue

                cursor.execute(
                    """
                    UPDATE opportunities
                    SET status = 'EXPIRED',
                        responded_at = CURRENT_TIMESTAMP
                    WHERE id = %s
                    """,
                    (opportunity["id"],),
                )

                cursor.execute(
                    """
                    INSERT INTO status_history (
                        lead_id,
                        contractor_id,
                        opportunity_id,
                        event_type,
                        previous_status,
                        new_status
                    )
                    VALUES (
                        %s, %s, %s,
                        'OpportunityExpired',
                        'PENDING',
                        'EXPIRED'
                    )
                    """,
                    (
                        candidate["lead_pk"],
                        opportunity["contractor_id"],
                        opportunity["id"],
                    ),
                )

                                # Record the event in the same transaction as expiration.
                # The SNS publisher can deliver it after this transaction commits.
                cursor.execute(
                    """
                    INSERT INTO outbox_events (
                        event_key,
                        event_type,
                        payload
                    )
                    VALUES (%s, %s, %s::jsonb)
                    ON CONFLICT (event_key) DO NOTHING
                    """,
                    (
                        (
                            "opportunity-expired:"
                            f"{opportunity['opportunity_id']}:refill"
                        ),
                        "OpportunityExpired",
                        json.dumps(
                            {
                                "lead_id": candidate["public_lead_id"],
                                "expired_opportunity_id": (
                                    opportunity["opportunity_id"]
                                ),
                            }
                        ),
                    ),
                )

                if cursor.rowcount == 1:
                    outbox_event_count += 1

                expired_ids.append(opportunity["opportunity_id"])

    # Commit expirations before attempting replacement routing.
    replacements: list[dict[str, Any]] = []



    return {
        "expired_count": len(expired_ids),
        "expired_opportunity_ids": expired_ids,
        "outbox_event_count": outbox_event_count,
    }


def _record_override(
    cursor: Any,
    *,
    opportunity: dict[str, Any],
    event_type: str,
    reason: str,
    performed_by: str,
    metadata: dict[str, Any] | None = None,
) -> None:
    """Record an administrative expiration action in the audit history."""

    cursor.execute(
        """
        INSERT INTO status_history (
            lead_id,
            contractor_id,
            opportunity_id,
            event_type,
            previous_status,
            new_status,
            metadata
        )
        VALUES (
            %s, %s, %s, %s,
            'PENDING', 'PENDING',
            jsonb_build_object(
                'reason', %s::text,
                'performed_by', %s::text,
                'details', %s::jsonb
            )
        )
        """,
        (
            opportunity["lead_pk"],
            opportunity["contractor_pk"],
            opportunity["opportunity_pk"],
            event_type,
            reason,
            performed_by,
            json.dumps(metadata or {}),
        ),
    )


def hold_opportunity(
    opportunity_id: str,
    *,
    reason: str,
    performed_by: str,
) -> dict[str, Any]:
    """Pause automatic expiration for a pending opportunity."""

    reason = reason.strip()
    performed_by = performed_by.strip()

    if not reason:
        raise ValueError("A reason is required to place a hold.")

    if not performed_by:
        raise ValueError("The administrator identity is required.")

    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT
                    o.id AS opportunity_pk,
                    o.opportunity_id,
                    o.lead_id AS lead_pk,
                    o.contractor_id AS contractor_pk,
                    o.status,
                    o.expiration_paused
                FROM opportunities o
                JOIN leads l ON l.id = o.lead_id
                WHERE o.opportunity_id = %s
                FOR UPDATE OF l, o
                """,
                (opportunity_id,),
            )
            opportunity = cursor.fetchone()

            if opportunity is None:
                raise LookupError("Opportunity not found.")

            if opportunity["status"] != "PENDING":
                raise ValueError(
                    "Only pending opportunities can be placed on hold."
                )

            if opportunity["expiration_paused"]:
                raise ValueError("This opportunity is already on hold.")

            cursor.execute(
                """
                UPDATE opportunities
                SET expiration_paused = TRUE,
                    expiration_override_reason = %s,
                    expiration_override_by = %s
                WHERE id = %s
                """,
                (reason, performed_by, opportunity["opportunity_pk"]),
            )

            _record_override(
                cursor,
                opportunity=opportunity,
                event_type="OpportunityExpirationHeld",
                reason=reason,
                performed_by=performed_by,
            )

    return {
        "opportunity_id": opportunity_id,
        "status": "PENDING",
        "expiration_paused": True,
        "reason": reason,
        "performed_by": performed_by,
    }


def extend_opportunity_deadline(
    opportunity_id: str,
    *,
    days: int,
    reason: str,
    performed_by: str,
) -> dict[str, Any]:
    """Extend a pending opportunity's deadline and record the override."""

    if not 1 <= days <= 30:
        raise ValueError("Extension must be between 1 and 30 days.")

    reason = reason.strip()
    performed_by = performed_by.strip()

    if not reason:
        raise ValueError("A reason is required to extend a deadline.")

    if not performed_by:
        raise ValueError("The administrator identity is required.")

    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT
                    o.id AS opportunity_pk,
                    o.opportunity_id,
                    o.lead_id AS lead_pk,
                    o.contractor_id AS contractor_pk,
                    o.status,
                    o.expires_at
                FROM opportunities o
                JOIN leads l ON l.id = o.lead_id
                WHERE o.opportunity_id = %s
                FOR UPDATE OF l, o
                """,
                (opportunity_id,),
            )
            opportunity = cursor.fetchone()

            if opportunity is None:
                raise LookupError("Opportunity not found.")

            if opportunity["status"] != "PENDING":
                raise ValueError(
                    "Only pending opportunities can be extended."
                )

            cursor.execute(
                """
                UPDATE opportunities
                SET expires_at =
                        GREATEST(expires_at, CURRENT_TIMESTAMP)
                        + (%s * INTERVAL '1 day'),
                    expiration_override_reason = %s,
                    expiration_override_by = %s
                WHERE id = %s
                RETURNING expires_at
                """,
                (
                    days,
                    reason,
                    performed_by,
                    opportunity["opportunity_pk"],
                ),
            )
            updated = cursor.fetchone()

            _record_override(
                cursor,
                opportunity=opportunity,
                event_type="OpportunityExpirationExtended",
                reason=reason,
                performed_by=performed_by,
                metadata={"extension_days": days},
            )

    return {
        "opportunity_id": opportunity_id,
        "status": "PENDING",
        "expires_at": updated["expires_at"].isoformat(),
        "extension_days": days,
        "reason": reason,
        "performed_by": performed_by,
    }


def release_opportunity_hold(
    opportunity_id: str,
    *,
    reason: str,
    performed_by: str,
) -> dict[str, Any]:
    """Resume automatic expiration monitoring for a held opportunity."""

    reason = reason.strip()
    performed_by = performed_by.strip()

    if not reason:
        raise ValueError("A reason is required to release a hold.")

    if not performed_by:
        raise ValueError("The administrator identity is required.")

    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT
                    o.id AS opportunity_pk,
                    o.opportunity_id,
                    o.lead_id AS lead_pk,
                    o.contractor_id AS contractor_pk,
                    o.status,
                    o.expiration_paused
                FROM opportunities o
                JOIN leads l ON l.id = o.lead_id
                WHERE o.opportunity_id = %s
                FOR UPDATE OF l, o
                """,
                (opportunity_id,),
            )
            opportunity = cursor.fetchone()

            if opportunity is None:
                raise LookupError("Opportunity not found.")

            if opportunity["status"] != "PENDING":
                raise ValueError(
                    "Only pending opportunities can have a hold released."
                )

            if not opportunity["expiration_paused"]:
                raise ValueError("This opportunity is not on hold.")

            cursor.execute(
                """
                UPDATE opportunities
                SET expiration_paused = FALSE,
                    expiration_override_reason = %s,
                    expiration_override_by = %s
                WHERE id = %s
                """,
                (reason, performed_by, opportunity["opportunity_pk"]),
            )

            _record_override(
                cursor,
                opportunity=opportunity,
                event_type="OpportunityExpirationHoldReleased",
                reason=reason,
                performed_by=performed_by,
            )

    return {
        "opportunity_id": opportunity_id,
        "status": "PENDING",
        "expiration_paused": False,
        "reason": reason,
        "performed_by": performed_by,
    }