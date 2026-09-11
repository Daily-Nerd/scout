from __future__ import annotations

from scout.rate_limit import github_rate_limited, request_with_backoff


class Response:
    def __init__(self, status_code, *, headers=None, payload=None):
        self.status_code = status_code
        self.headers = headers or {}
        self.payload = payload

    def json(self):
        return self.payload


def test_backoff_uses_retry_after_and_stops_after_success():
    responses = [
        Response(429, headers={"Retry-After": "7"}),
        Response(200),
    ]
    sleeps: list[float] = []

    result = request_with_backoff(
        lambda: responses.pop(0),
        should_retry=lambda response: response.status_code == 429,
        sleep=sleeps.append,
        base_delay=2,
    )

    assert result.status_code == 200
    assert sleeps == [7.0]


def test_github_403_permission_error_is_not_rate_limited():
    assert not github_rate_limited(
        Response(403, payload={"message": "Resource not accessible by integration"})
    )


def test_github_403_secondary_limit_is_rate_limited():
    assert github_rate_limited(
        Response(403, payload={"message": "You have exceeded a secondary rate limit"})
    )
