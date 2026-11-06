"""Stable, deterministic tokens for homeowner review invitations."""

import hashlib
import hmac
import os


def _secret() -> bytes:
    secret = os.getenv("EMG_REVIEW_TOKEN_SECRET", "").strip()
    if not secret:
        raise RuntimeError("EMG_REVIEW_TOKEN_SECRET is not configured.")
    return secret.encode("utf-8")


def generate_review_token(invitation_id: int) -> str:
    """Derive the same unguessable token for an invitation on every retry."""
    message = f"homeowner-review:v1:{invitation_id}".encode("utf-8")
    return hmac.new(_secret(), message, hashlib.sha256).hexdigest()


def hash_review_token(token: str) -> str:
    """Return the SHA-256 digest stored in the invitation table."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def review_token_matches(token: str, stored_hash: str) -> bool:
    """Compare a submitted token with its stored digest."""
    return hmac.compare_digest(hash_review_token(token), stored_hash)