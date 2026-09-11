"""Retract a misfired candidate batch.

Walks every open candidate issue in the target repo, labels it
scout:retracted and closes it, at most one write every
config.issues.request_interval_seconds. The run is resumable because
closed issues drop out of the open filter on the next pass. Nothing is
deleted: the closed issues stay as the dedupe ledger. Dry by default;
--apply writes, and only with --apply does the explainer issue get created
and pinned.
"""

from __future__ import annotations

import sys
import time
from dataclasses import dataclass, field

import requests

from .config import Config
from .github_search import TOKEN_ENV, TokenMissingError
from .issues import _headers
from .rate_limit import github_rate_limited, request_with_backoff

API = "https://api.github.com"
RETRACTED_LABEL = "scout:retracted"
RETRACTED_COLOR = "b60205"
EXPLAINER_TITLE = "scout: the first candidate batch was retracted"

EXPLAINER_BODY = """\
Scout misfired and filed about 1255 candidate issues in one night. The
search matched repos where the words agent and memory appeared anywhere in
the name or description, and it only looked at repos created in the last
two weeks, so almost everything in that batch was noise.

Every issue in the batch is now closed under the scout:retracted label.
None of them were deleted; they stay as the ledger so nothing from the bad
batch is ever refiled.

The filter is fixed: the phrase is quoted, the created window is gone, and
every hit passes the same term gate the Reddit source always ran. Issues
filed after this one come from the fixed pipeline, and a person still
decides each one.
"""


@dataclass
class RetractResult:
    apply: bool
    found: int = 0
    closed: int = 0
    issues: list[tuple[int, str]] = field(default_factory=list)
    explainer_number: int | None = None
    pinned: bool = False


def _open_candidate_issues(
    session: requests.Session,
    config: Config,
    headers: dict[str, str],
    *,
    sleep=time.sleep,
) -> list[tuple[int, str, list[str]]]:
    """(number, title, labels) for every open candidate issue.

    The issues API stops paginating past 1000 results, so the list is
    walked twice: newest first, then oldest first, deduped by number.
    """
    found: dict[int, tuple[int, str, list[str]]] = {}
    for direction in ("desc", "asc"):
        for page in range(1, 11):
            response = request_with_backoff(
                lambda: session.get(
                    f"{API}/repos/{config.issues.target_repo}/issues",
                    params={
                        "state": "open",
                        "labels": config.issues.label,
                        "sort": "created",
                        "direction": direction,
                        "per_page": 100,
                        "page": page,
                    },
                    headers=headers,
                    timeout=30,
                ),
                should_retry=github_rate_limited,
                sleep=sleep,
                max_retries=config.issues.max_rate_limit_retries,
            )
            response.raise_for_status()
            items = response.json()
            for item in items:
                if item.get("pull_request"):
                    continue
                labels = [label.get("name", "") for label in item.get("labels", [])]
                found[item["number"]] = (
                    item["number"], item.get("title", ""), labels,
                )
            if len(items) < 100:
                break
    ordered = sorted(found.values(), key=lambda entry: entry[0])
    return [(number, title, labels) for number, title, labels in ordered]


def _ensure_retracted_label(
    session: requests.Session,
    config: Config,
    headers: dict[str, str],
    *,
    sleep=time.sleep,
) -> None:
    labels_url = f"{API}/repos/{config.issues.target_repo}/labels"
    response = request_with_backoff(
        lambda: session.get(
            f"{labels_url}/{RETRACTED_LABEL}", headers=headers, timeout=30
        ),
        should_retry=github_rate_limited,
        sleep=sleep,
        max_retries=config.issues.max_rate_limit_retries,
    )
    if response.status_code == 200:
        return
    if response.status_code != 404:
        response.raise_for_status()
    create = request_with_backoff(
        lambda: session.post(
            labels_url,
            headers=headers,
            json={"name": RETRACTED_LABEL, "color": RETRACTED_COLOR},
            timeout=30,
        ),
        should_retry=github_rate_limited,
        sleep=sleep,
        max_retries=config.issues.max_rate_limit_retries,
    )
    create.raise_for_status()


def retract(
    config: Config,
    *,
    apply: bool,
    token: str | None,
    session: requests.Session | None = None,
    sleep=time.sleep,
) -> RetractResult:
    """Close every open candidate issue under scout:retracted.

    Dry by default: returns what would close. With apply, writes at most
    one per config.issues.request_interval_seconds, then files and pins the
    explainer issue.
    """
    if apply and not token:
        raise TokenMissingError(
            f"{TOKEN_ENV} is required to retract issues; "
            "set it in the environment or in .env"
        )
    if session is None:
        session = requests.Session()
    headers = _headers(token)

    issues = _open_candidate_issues(session, config, headers, sleep=sleep)
    result = RetractResult(apply=apply, found=len(issues), issues=issues)
    if not apply or not issues:
        return result

    _ensure_retracted_label(session, config, headers, sleep=sleep)
    last_write_at = 0.0
    for number, _title, labels in issues:
        wait = config.issues.request_interval_seconds - (
            time.monotonic() - last_write_at
        )
        if wait > 0:
            sleep(wait)
        if RETRACTED_LABEL not in labels:
            labels = [*labels, RETRACTED_LABEL]
        response = request_with_backoff(
            lambda: session.patch(
                f"{API}/repos/{config.issues.target_repo}/issues/{number}",
                headers=headers,
                json={"state": "closed", "labels": labels},
                timeout=30,
            ),
            should_retry=github_rate_limited,
            sleep=sleep,
            max_retries=config.issues.max_rate_limit_retries,
        )
        response.raise_for_status()
        last_write_at = time.monotonic()
        result.closed += 1

    wait = config.issues.request_interval_seconds - (
        time.monotonic() - last_write_at
    )
    if wait > 0:
        sleep(wait)
    create = request_with_backoff(
        lambda: session.post(
            f"{API}/repos/{config.issues.target_repo}/issues",
            headers=headers,
            json={"title": EXPLAINER_TITLE, "body": EXPLAINER_BODY,
                  "labels": [RETRACTED_LABEL]},
            timeout=30,
        ),
        should_retry=github_rate_limited,
        sleep=sleep,
        max_retries=config.issues.max_rate_limit_retries,
    )
    create.raise_for_status()
    result.explainer_number = create.json()["number"]
    last_write_at = time.monotonic()

    wait = config.issues.request_interval_seconds - (
        time.monotonic() - last_write_at
    )
    if wait > 0:
        sleep(wait)
    pin = session.put(
        f"{API}/repos/{config.issues.target_repo}/issues/"
        f"{result.explainer_number}/pin",
        headers=headers,
        timeout=30,
    )
    result.pinned = pin.status_code in (200, 204)
    if not result.pinned:
        print(
            f"retract: could not pin issue #{result.explainer_number} "
            f"(status {pin.status_code}); the issue is still open and labeled",
            file=sys.stderr,
        )
    return result
