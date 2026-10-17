import secrets

import pytest

from app.database.connection import get_connection
from app.services.login_rate_limit import (
    _identifiers,
    is_login_rate_limited,
    record_login_failure,
)


@pytest.fixture
def rate_limit_identity():
    """Give each test unique identifiers and clean up its database rows."""
    suffix = secrets.token_hex(8)
    email = f"rate-limit-{suffix}@example.com"
    client_ip = f"198.51.100.{int(suffix[:2], 16)}"

    email_ip_hash, ip_hash = _identifiers(email, client_ip)

    yield email, client_ip

    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                DELETE FROM login_rate_limits
                WHERE (scope = 'EMAIL_IP' AND key_hash = %s)
                   OR (scope = 'IP' AND key_hash = %s)
                """,
                (email_ip_hash, ip_hash),
            )


def test_login_is_not_limited_before_failures(rate_limit_identity):
    email, client_ip = rate_limit_identity

    assert not is_login_rate_limited(
        email=email,
        client_ip=client_ip,
    )


def test_five_failures_trigger_email_ip_limit(rate_limit_identity):
    email, client_ip = rate_limit_identity

    for _ in range(5):
        record_login_failure(email=email, client_ip=client_ip)

    assert is_login_rate_limited(
        email=email,
        client_ip=client_ip,
    )


def test_ip_limit_applies_across_different_emails(rate_limit_identity):
    _, client_ip = rate_limit_identity

    for number in range(30):
        record_login_failure(
            email=f"account-{number}-{secrets.token_hex(4)}@example.com",
            client_ip=client_ip,
        )

    assert is_login_rate_limited(
        email=f"another-account-{secrets.token_hex(4)}@example.com",
        client_ip=client_ip,
    )


def test_expired_window_resets_failure_count(rate_limit_identity):
    email, client_ip = rate_limit_identity

    for _ in range(5):
        record_login_failure(email=email, client_ip=client_ip)

    email_ip_hash, ip_hash = _identifiers(email, client_ip)

    with get_connection() as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                """
                UPDATE login_rate_limits
                SET window_started_at =
                    CURRENT_TIMESTAMP - INTERVAL '16 minutes'
                WHERE (scope = 'EMAIL_IP' AND key_hash = %s)
                   OR (scope = 'IP' AND key_hash = %s)
                """,
                (email_ip_hash, ip_hash),
            )

    assert not is_login_rate_limited(
        email=email,
        client_ip=client_ip,
    )

    record_login_failure(email=email, client_ip=client_ip)

    assert not is_login_rate_limited(
        email=email,
        client_ip=client_ip,
    )