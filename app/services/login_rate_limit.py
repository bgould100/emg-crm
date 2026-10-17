
"""PostgreSQL-backed rate limiting for contractor login failures."""

import hashlib
import hmac
import os
from datetime import timedelta

from app.database.connection import get_connection


WINDOW = timedelta(minutes=15)
EMAIL_IP_LIMIT = 5
IP_LIMIT = 30


def _key_hash(scope: str, value: str) -> str:
    """Create a keyed hash so raw identifiers aren't stored in the database."""
    secret = os.getenv("EMG_RATE_LIMIT_SECRET", "").strip()
    if not secret:
        raise RuntimeError("EMG_RATE_LIMIT_SECRET is not configured.")

    message = f"{scope}:{value}".encode("utf-8")
    return hmac.new(
        secret.encode("utf-8"),
        message,
        hashlib.sha256,
    ).hexdigest()


def _identifiers(email: str, client_ip: str) -> tuple[str, str]:
    normalized_email = email.strip().lower()
    normalized_ip = client_ip.strip() or "unknown"

    email_ip_hash = _key_hash(
        "EMAIL_IP",
        f"{normalized_email}\0{normalized_ip}",
    )
    ip_hash = _key_hash("IP", normalized_ip)

    return email_ip_hash, ip_hash


def record_login_failure(*, email: str, client_ip: str) -> None:
    """Record a failed login against both the email/IP pair and the IP."""

    email_ip_hash, ip_hash = _identifiers(email, client_ip)

    with get_connection() as conn:
        with conn.cursor() as cursor:
            for scope, key_hash in (
                ("EMAIL_IP", email_ip_hash),
                ("IP", ip_hash),
            ):
                cursor.execute(
                    """
                    INSERT INTO login_rate_limits
                        (scope, key_hash, attempts, window_started_at, updated_at)
                    VALUES (%s, %s, 1, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
                    ON CONFLICT (scope, key_hash)
                    DO UPDATE SET
                        attempts = CASE
                            WHEN login_rate_limits.window_started_at
                                 <= CURRENT_TIMESTAMP - %s::interval
                            THEN 1
                            ELSE login_rate_limits.attempts + 1
                        END,
                        window_started_at = CASE
                            WHEN login_rate_limits.window_started_at
                                 <= CURRENT_TIMESTAMP - %s::interval
                            THEN CURRENT_TIMESTAMP
                            ELSE login_rate_limits.window_started_at
                        END,
                        updated_at = CURRENT_TIMESTAMP
                    """,
                    (scope, key_hash, WINDOW, WINDOW),
                )

            # Opportunistically clean up old rate-limit records.
            cursor.execute(
                """
                DELETE FROM login_rate_limits
                WHERE updated_at < CURRENT_TIMESTAMP - INTERVAL '24 hours'
                """
            )


def _attempts_in_window(cursor, *, scope: str, key_hash: str) -> int:
    """Return the active-window attempt count for one identifier."""

    cursor.execute(
        """
        SELECT attempts, window_started_at
        FROM login_rate_limits
        WHERE scope = %s AND key_hash = %s
        """,
        (scope, key_hash),
    )
    row = cursor.fetchone()

    if row is None:
        return 0

    cursor.execute(
        """
        SELECT %s::timestamptz <= CURRENT_TIMESTAMP - %s::interval
            AS expired
        """,
        (row["window_started_at"], WINDOW),
    )
    if cursor.fetchone()["expired"]:
        return 0

    return row["attempts"]


def is_login_rate_limited(*, email: str, client_ip: str) -> bool:
    """Check whether failed login attempts have reached either limit."""

    email_ip_hash, ip_hash = _identifiers(email, client_ip)

    with get_connection() as conn:
        with conn.cursor() as cursor:
            email_ip_attempts = _attempts_in_window(
                cursor,
                scope="EMAIL_IP",
                key_hash=email_ip_hash,
            )
            ip_attempts = _attempts_in_window(
                cursor,
                scope="IP",
                key_hash=ip_hash,
            )

    return (
        email_ip_attempts >= EMAIL_IP_LIMIT
        or ip_attempts >= IP_LIMIT
    )
