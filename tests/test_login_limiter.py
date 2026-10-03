import pytest
from fastapi import HTTPException

from cabinet.security.login_limiter import LoginRateLimiter


def test_login_limiter_caps_account_and_address_and_returns_retry_after():
    limiter = LoginRateLimiter(window_seconds=900)
    for _ in range(2):
        limiter.consume(
            scope="student",
            client_ip="203.0.113.10",
            username="student_one",
            account_limit=2,
            address_limit=3,
        )

    with pytest.raises(HTTPException) as account_limit:
        limiter.consume(
            scope="student",
            client_ip="203.0.113.10",
            username="STUDENT_ONE",
            account_limit=2,
            address_limit=3,
        )
    assert account_limit.value.status_code == 429
    assert 1 <= int(account_limit.value.headers["Retry-After"]) <= 900

    limiter.consume(
        scope="student",
        client_ip="203.0.113.10",
        username="student_two",
        account_limit=2,
        address_limit=3,
    )
    with pytest.raises(HTTPException) as address_limit:
        limiter.consume(
            scope="student",
            client_ip="203.0.113.10",
            username="student_three",
            account_limit=2,
            address_limit=3,
        )
    assert address_limit.value.status_code == 429


def test_success_reset_clears_only_account_bucket_and_scope_is_separate():
    limiter = LoginRateLimiter()
    for _ in range(2):
        limiter.consume(
            scope="student",
            client_ip="203.0.113.11",
            username="student_one",
            account_limit=2,
            address_limit=4,
        )

    limiter.reset_account(
        scope="student", client_ip="203.0.113.11", username="student_one"
    )
    limiter.consume(
        scope="student",
        client_ip="203.0.113.11",
        username="student_one",
        account_limit=2,
        address_limit=4,
    )
    limiter.consume(
        scope="admin",
        client_ip="203.0.113.11",
        username="teacher",
        account_limit=2,
        address_limit=4,
    )
