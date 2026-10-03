"""Process-local rate limits for student and administrator logins."""

from __future__ import annotations

from collections import deque
from math import ceil
from threading import Lock
from time import monotonic

from fastapi import HTTPException, Request


class LoginRateLimiter:
    def __init__(self, *, window_seconds: int = 900, max_buckets: int = 8192):
        self.window_seconds = window_seconds
        self.max_buckets = max_buckets
        self._attempts: dict[tuple[str, ...], deque[float]] = {}
        self._lock = Lock()
        self._last_prune = 0.0

    def consume(
        self,
        *,
        scope: str,
        client_ip: str,
        username: str,
        account_limit: int,
        address_limit: int,
    ) -> None:
        """Count a login request and reject it when either rolling window is full."""
        now = monotonic()
        address_key = (scope, "ip", client_ip)
        account_key = (scope, "account", client_ip, username.casefold())
        with self._lock:
            if now - self._last_prune >= 60 or len(self._attempts) > self.max_buckets:
                self._prune(now)
                self._last_prune = now
            address_attempts = self._attempts.setdefault(address_key, deque())
            account_attempts = self._attempts.setdefault(account_key, deque())
            self._prune_bucket(address_attempts, now)
            self._prune_bucket(account_attempts, now)
            if not address_attempts:
                address_attempts.clear()
            if not account_attempts:
                account_attempts.clear()
            limits = ((address_attempts, address_limit), (account_attempts, account_limit))
            retry_after = max(
                (
                    ceil(self.window_seconds - (now - bucket[0]))
                    for bucket, limit in limits
                    if len(bucket) >= limit
                ),
                default=0,
            )
            if retry_after:
                raise HTTPException(
                    status_code=429,
                    detail="Слишком много попыток входа. Подождите и попробуйте снова.",
                    headers={"Retry-After": str(max(1, retry_after))},
                )
            address_attempts.append(now)
            account_attempts.append(now)

    def reset_account(self, *, scope: str, client_ip: str, username: str) -> None:
        """Clear the account-specific bucket after a successful login."""
        with self._lock:
            self._attempts.pop((scope, "account", client_ip, username.casefold()), None)

    def _prune(self, now: float) -> None:
        for key, bucket in list(self._attempts.items()):
            self._prune_bucket(bucket, now)
            if not bucket:
                del self._attempts[key]

        if len(self._attempts) > self.max_buckets:
            oldest = sorted(
                self._attempts,
                key=lambda key: self._attempts[key][0],
            )
            for key in oldest[: len(self._attempts) - self.max_buckets]:
                del self._attempts[key]

    def _prune_bucket(self, bucket: deque[float], now: float) -> None:
        while bucket and now - bucket[0] >= self.window_seconds:
            bucket.popleft()


login_rate_limiter = LoginRateLimiter()


def request_client_ip(request: Request) -> str:
    """Return the peer IP after Uvicorn has accepted trusted proxy headers."""
    return request.client.host if request.client else "unknown"
