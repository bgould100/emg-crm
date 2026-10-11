
import hashlib
import secrets
from datetime import timedelta
from typing import Any

from pwdlib import PasswordHash
from psycopg import IntegrityError

from app.database.connection import get_connection


password_hasher = PasswordHash.recommended()

SESSION_LIFETIME = timedelta(hours=12)


def _normalize_email(email: str) -> str:
    """Normalize an email address for consistent account lookup."""
    return email.strip().lower()


def _hash_session_token(token: str) -> str:
    """Hash a session token before it is stored in the database."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def provision_contractor_account(
    *,
    contractor_id: str,
    email: str,
    password: str,
) -> dict[str, Any]:
    """Create a login for an existing, active contractor."""

    normalized_email = _normalize_email(email)

    if not normalized_email or "@" not in normalized_email:
        raise ValueError("Enter a valid email address.")

    if len(password) < 12:
        raise ValueError("Password must contain at least 12 characters.")

    password_hash = password_hasher.hash(password)

    try:
        with get_connection() as connection:
            with connection.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT id, email, status
                    FROM contractors
                    WHERE contractor_id = %s
                    """,
                    (contractor_id.strip(),),
                )
                contractor = cursor.fetchone()

                if contractor is None:
                    raise ValueError("Contractor profile not found.")

                if contractor["status"] != "ACTIVE":
                    raise ValueError("Contractor profile is not active.")

                if _normalize_email(contractor["email"]) != normalized_email:
                    raise ValueError(
                        "Login email must match the contractor profile email."
                    )

                cursor.execute(
                    """
                    INSERT INTO contractor_accounts (
                        contractor_id,
                        email,
                        password_hash
                    )
                    VALUES (%s, %s, %s)
                    RETURNING id, email, status
                    """,
                    (
                        contractor["id"],
                        normalized_email,
                        password_hash,
                    ),
                )
                account = cursor.fetchone()

        return account

    except IntegrityError as error:
        raise ValueError(
            "An account already exists for this contractor or email."
        ) from error


def login_contractor(
    *,
    email: str,
    password: str,
) -> dict[str, Any]:
    """Verify credentials and create a server-tracked session."""

    normalized_email = _normalize_email(email)

    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT
                    a.id AS account_id,
                    a.email,
                    a.password_hash,
                    a.status AS account_status,
                    c.id AS contractor_pk,
                    c.contractor_id,
                    c.company_name,
                    c.status AS contractor_status
                FROM contractor_accounts a
                JOIN contractors c ON c.id = a.contractor_id
                WHERE LOWER(a.email) = %s
                """,
                (normalized_email,),
            )
            account = cursor.fetchone()

            if (
                account is None
                or account["account_status"] != "ACTIVE"
                or account["contractor_status"] != "ACTIVE"
            ):
                raise ValueError("Invalid email or password.")

            if not password_hasher.verify(
                password,
                account["password_hash"],
            ):
                raise ValueError("Invalid email or password.")

            # Return this raw token to the client once.
            # Only its SHA-256 hash is persisted.
            session_token = secrets.token_urlsafe(32)
            token_hash = _hash_session_token(session_token)

            cursor.execute(
                """
                INSERT INTO contractor_sessions (
                    account_id,
                    token_hash,
                    expires_at
                )
                VALUES (
                    %s,
                    %s,
                    CURRENT_TIMESTAMP + %s
                )
                """,
                (
                    account["account_id"],
                    token_hash,
                    SESSION_LIFETIME,
                ),
            )

    return {
        "session_token": session_token,
        "expires_in": int(SESSION_LIFETIME.total_seconds()),
        "account_id": account["account_id"],
        "contractor_pk": account["contractor_pk"],
        "contractor_id": account["contractor_id"],
        "company_name": account["company_name"],
        "email": account["email"],
    }


def authenticate_contractor_session(
    session_token: str,
) -> dict[str, Any] | None:
    """Validate a session and return the authenticated contractor."""

    if not session_token:
        return None

    token_hash = _hash_session_token(session_token)

    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                SELECT
                    a.id AS account_id,
                    a.email,
                    c.id AS contractor_pk,
                    c.contractor_id,
                    c.company_name
                FROM contractor_sessions s
                JOIN contractor_accounts a ON a.id = s.account_id
                JOIN contractors c ON c.id = a.contractor_id
                WHERE s.token_hash = %s
                  AND s.revoked_at IS NULL
                  AND s.expires_at > CURRENT_TIMESTAMP
                  AND a.status = 'ACTIVE'
                  AND c.status = 'ACTIVE'
                """,
                (token_hash,),
            )
            account = cursor.fetchone()

            if account is None:
                return None

            cursor.execute(
                """
                UPDATE contractor_sessions
                SET last_used_at = CURRENT_TIMESTAMP
                WHERE token_hash = %s
                """,
                (token_hash,),
            )

    return account


def logout_contractor(session_token: str) -> None:
    """Revoke a session without storing or logging its raw token."""

    if not session_token:
        return

    token_hash = _hash_session_token(session_token)

    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE contractor_sessions
                SET revoked_at = CURRENT_TIMESTAMP
                WHERE token_hash = %s
                  AND revoked_at IS NULL
                """,
                (token_hash,),
            )
