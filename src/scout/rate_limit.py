"""Small, bounded HTTP retry helpers for public APIs."""

from __future__ import annotations

import time
from collections.abc import Callable


DEFAULT_MAX_RETRIES = 3
DEFAULT_BACKOFF_SECONDS = 5.0


def retry_after(response, fallback: float) -> float:
    """Return a server-provided retry delay, or an exponential fallback."""
    headers = getattr(response, "headers", {}) or {}
    value = headers.get("Retry-After")
    if value is not None:
        try:
            return max(0.0, float(value))
        except (TypeError, ValueError):
            pass
    return fallback


def github_rate_limited(response) -> bool:
    """Recognise GitHub rate-limit responses without masking permission errors."""
    if response.status_code == 429:
        return True
    if response.status_code != 403:
        return False
    headers = getattr(response, "headers", {}) or {}
    if headers.get("Retry-After") is not None:
        return True
    if headers.get("X-RateLimit-Remaining") == "0":
        return True
    try:
        message = str(response.json().get("message", "")).lower()
    except (AttributeError, TypeError, ValueError):
        message = ""
    return any(term in message for term in ("rate limit", "abuse", "secondary"))


def request_with_backoff(
    request: Callable[[], object],
    *,
    should_retry: Callable[[object], bool],
    sleep: Callable[[float], None] = time.sleep,
    max_retries: int = DEFAULT_MAX_RETRIES,
    base_delay: float = DEFAULT_BACKOFF_SECONDS,
) -> object:
    """Retry only rate-limited responses, with bounded exponential backoff."""
    for attempt in range(max_retries + 1):
        response = request()
        if not should_retry(response) or attempt == max_retries:
            return response
        sleep(retry_after(response, base_delay * (2**attempt)))
    raise AssertionError("unreachable")
