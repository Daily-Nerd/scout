"""GitHub repository search source.

Queries the search API with the configured query list, filtered to repos
created and pushed inside the configured window. Forks and archived repos
are excluded, and every repo returned is persisted in the local store so a
rerun only surfaces new repos.
"""

from __future__ import annotations

import os
import time
from datetime import UTC, datetime, timedelta

import requests

from .config import Config
from .models import Candidate, normalize_repo
from .rate_limit import github_rate_limited, request_with_backoff
from .store import Store

SEARCH_URL = "https://api.github.com/search/repositories"
TOKEN_ENV = "SCOUT_GITHUB_TOKEN"
PER_PAGE = 100
PAGE_PAUSE_SECONDS = 2.0


class TokenMissingError(RuntimeError):
    """Raised when the GitHub source runs without a token."""


def _headers(token: str) -> dict[str, str]:
    return {
        "Accept": "application/vnd.github+json",
        "Authorization": f"Bearer {token}",
        "User-Agent": "scout",
    }


def _to_candidate(item: dict, query: str) -> Candidate | None:
    if item.get("fork") or item.get("archived"):
        return None
    html_url = item.get("html_url", "")
    try:
        repo = normalize_repo(item.get("full_name") or html_url)
    except ValueError:
        return None
    license_info = item.get("license") or {}
    return Candidate(
        repo=repo,
        source="github",
        source_url=html_url,
        matched_terms=[query],
        description=item.get("description") or "",
        stars=item.get("stargazers_count"),
        pushed_at=item.get("pushed_at"),
        license=license_info.get("spdx_id") or None,
        html_url=html_url or None,
    )


def search(
    config: Config,
    store: Store,
    *,
    token: str | None = None,
    session: requests.Session | None = None,
    sleep=time.sleep,
) -> list[Candidate]:
    """Run every configured query and return unseen repos as candidates.

    Paginates with per_page=100 and pauses between pages to stay inside the
    search rate limit. Repos already in the store are treated as ingested and
    skipped; new repos are marked seen as they are collected.
    """
    if token is None:
        token = os.environ.get(TOKEN_ENV)
    if not token:
        raise TokenMissingError(
            f"{TOKEN_ENV} is required for the github source; "
            "set it in the environment or in .env"
        )
    if session is None:
        session = requests.Session()

    cutoff = datetime.now(UTC).date() - timedelta(days=config.github.window_days)
    window = f" created:>{cutoff.isoformat()} pushed:>{cutoff.isoformat()}"
    headers = _headers(token)

    candidates: list[Candidate] = []
    for query in config.github.queries:
        for page in range(1, 11):  # the search API caps results at 1000
            response = request_with_backoff(
                lambda: session.get(
                    SEARCH_URL,
                    params={
                        "q": query + window,
                        "sort": "updated",
                        "order": "desc",
                        "per_page": PER_PAGE,
                        "page": page,
                    },
                    headers=headers,
                    timeout=30,
                ),
                should_retry=github_rate_limited,
                sleep=sleep,
            )
            response.raise_for_status()
            items = response.json().get("items", [])

            unseen_on_page = 0
            for item in items:
                candidate = _to_candidate(item, query)
                if candidate is None or store.is_repo_seen(candidate.repo):
                    continue
                store.mark_repo_seen(candidate.repo, "github")
                candidates.append(candidate)
                unseen_on_page += 1

            if not items or len(items) < PER_PAGE or unseen_on_page == 0:
                break
            sleep(PAGE_PAUSE_SECONDS)

    return candidates
