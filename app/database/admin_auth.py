
"""Database-backed authentication for EMG administrators."""

import hashlib
import secrets
from datetime import timedelta
from typing import Any

from psycopg import IntegrityError

from app.database.auth import password_hasher
from app.database.connection import get_connection


ADMIN_SESSION_LIFETIME = timedelta(hours=12)


def normalize_username(username: str) -> str:
    return username.strip().lower()


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def create_admin_user(*, username: str, password: str) -> dict[str, Any]:
    username = normalize_username(username)

    if not 3 <= len(username) <= 64:
        raise ValueError("Username must contain 3 to 64 characters.")

    if any(char.isspace() for char in username):
        raise ValueError("Username cannot contain spaces.")

    if len(password) < 12:
        raise ValueError("Password must contain at least 12 characters.")

    password_hash = password_hasher.hash(password)

    try:
        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    INSERT INTO admin_users (username, password_hash)
                    VALUES (%s, %s)
                    RETURNING id, username, status
                    """,
                    (username, password_hash),
                )
                return cur.fetchone()
    except IntegrityError as exc:
        raise ValueError(
            "That username already exists or cannot be created."
        ) from exc


def login_admin(*, username: str, password: str) -> dict[str, Any]:
    username = normalize_username(username)

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, username, password_hash, status
                FROM admin_users
                WHERE username = %s
                """,
                (username,),
            )
            user = cur.fetchone()

            if user is None or user["status"] != "ACTIVE":
                raise ValueError("Invalid username or password.")

            if not password_hasher.verify(password, user["password_hash"]):
                raise ValueError("Invalid username or password.")

            token = secrets.token_urlsafe(32)

            cur.execute(
                """
                INSERT INTO admin_sessions (
                    admin_user_id, token_hash, expires_at
                )
                VALUES (%s, %s, CURRENT_TIMESTAMP + %s)
                """,
                (user["id"], _hash_token(token), ADMIN_SESSION_LIFETIME),
            )

    return {
        "session_token": token,
        "expires_in": int(ADMIN_SESSION_LIFETIME.total_seconds()),
        "admin_id": user["id"],
        "username": user["username"],
    }


def authenticate_admin_session(token: str) -> dict[str, Any] | None:
    if not token:
        return None

    token_hash = _hash_token(token)

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT u.id AS admin_id, u.username
                FROM admin_sessions s
                JOIN admin_users u ON u.id = s.admin_user_id
                WHERE s.token_hash = %s
                  AND s.revoked_at IS NULL
                  AND s.expires_at > CURRENT_TIMESTAMP
                  AND u.status = 'ACTIVE'
                """,
                (token_hash,),
            )
            user = cur.fetchone()

            if user is None:
                return None

            cur.execute(
                """
                UPDATE admin_sessions
                SET last_used_at = CURRENT_TIMESTAMP
                WHERE token_hash = %s
                """,
                (token_hash,),
            )

    return user


def logout_admin(token: str) -> None:
    if not token:
        return

    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE admin_sessions
                SET revoked_at = CURRENT_TIMESTAMP
                WHERE token_hash = %s
                  AND revoked_at IS NULL
                """,
                (_hash_token(token),),
            )
