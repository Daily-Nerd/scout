"""Metadata refresh pass.

Rows discovered a while ago accumulate stale metadata: a Reddit hit never
had stars or pushed_at to begin with, and a GitHub hit's stars, recency
and topics only reflect the moment it was first seen. Each run, at most
config.refresh.max_per_run rows whose score is absent or older than
config.refresh.days get a fresh GET /repos/{repo}, a forced (cache
bypassing) tree and README fetch, and a rescore. The tier a rescore
lands on can differ from what is on file, and when the row is already
filed, that issue's tier label is swapped to match.

Refresh never files an issue and never touches an issue beyond that one
label swap.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field, fields
from datetime import UTC, datetime

import requests

from . import issues as issues_mod
from .config import Config
from .issues import _headers
from .models import Candidate
from .rate_limit import github_rate_limited, request_with_backoff
from .store import Store
from .tiering import TIER_LABELS, _fetch_readme_size, _fetch_tree_signals, score_candidate

API = "https://api.github.com"

_CANDIDATE_FIELDS = {candidate_field.name for candidate_field in fields(Candidate)}


@dataclass
class RefreshOutcome:
    """What one refresh pass did."""

    refreshed: list[str] = field(default_factory=list)
    tier_changes: list[tuple[str, str, str]] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)


def _candidate_from_latest(latest: dict) -> Candidate:
    payload = {key: value for key, value in latest.items() if key in _CANDIDATE_FIELDS}
    return Candidate(**payload)


def refresh(
    config: Config,
    store: Store,
    *,
    token: str | None,
    session: requests.Session | None = None,
    sleep=time.sleep,
    now: datetime | None = None,
    apply: bool = False,
) -> RefreshOutcome:
    """Rescore stale rows, at most config.refresh.max_per_run of them.

    Each due row gets a fresh GET /repos/{repo}. A 404 is recorded in
    `failed` and the row is left exactly as it was, with no further
    fetch and no rescore. Otherwise `latest` is updated with the fresh
    stars, pushed_at, description, topics, license and html_url, the
    row's source is left untouched (a title_only row, which has no
    latest yet, has its source and source_url filled in instead, from
    the row's sources and the fetched html_url), the git tree and
    README are refetched unconditionally, bypassing the usual cache,
    and the row is rescored and re-recorded. When the resulting tier
    differs from what was on file and the row already has a filed
    issue, that issue's tier label is swapped to match.
    """
    if store.history is None:
        return RefreshOutcome()
    if session is None:
        session = requests.Session()
    now = now or datetime.now(UTC)
    headers = _headers(token)
    outcome = RefreshOutcome()

    due = store.rows_due_for_refresh(now, config.refresh.days, config.refresh.max_per_run)

    for repo in due:
        row = store.history.repos.get(repo)
        if row is None:
            continue

        response = request_with_backoff(
            lambda: session.get(f"{API}/repos/{repo}", headers=headers, timeout=30),
            should_retry=github_rate_limited,
            sleep=sleep,
        )
        if response.status_code == 404:
            outcome.failed.append(repo)
            sleep(2)
            continue
        response.raise_for_status()
        payload = response.json()

        extra: dict[str, object] = {}
        if not isinstance(row.get("latest"), dict):
            sources = row.get("sources") or []
            extra["source"] = sources[0] if sources else "github"
            extra["source_url"] = payload.get("html_url") or f"https://github.com/{repo}"

        license_info = payload.get("license") or {}
        store.update_latest(
            repo,
            stars=payload.get("stargazers_count"),
            pushed_at=payload.get("pushed_at"),
            description=payload.get("description") or "",
            topics=list(payload.get("topics") or []),
            license=license_info.get("spdx_id"),
            html_url=payload.get("html_url"),
            **extra,
        )

        tree_signals = _fetch_tree_signals(
            session, repo, headers,
            extensions=config.tiering.source_extensions, sleep=sleep,
        )
        if tree_signals is not None:
            has_tests, source_files = tree_signals
            fetched_at = now.strftime("%Y-%m-%dT%H:%M:%SZ")
            store.save_tree_signals(repo, has_tests, source_files, fetched_at)
        else:
            has_tests, source_files = None, None

        readme_bytes = _fetch_readme_size(session, repo, headers, sleep=sleep)
        store.save_readme_size(repo, readme_bytes)

        candidate = _candidate_from_latest(row["latest"])
        old_tier = row.get("tier")
        score = score_candidate(
            config, candidate,
            readme_bytes=readme_bytes,
            tree_tests=has_tests,
            tree_source_files=source_files,
            now=now,
        )
        store.record_score(repo, score)
        outcome.refreshed.append(repo)

        issue_number = row.get("issue_number")
        new_tier = score.tier
        if issue_number and old_tier and old_tier != new_tier:
            issues_mod.update_tier_label(
                config, session, token, issue_number,
                TIER_LABELS[old_tier], TIER_LABELS[new_tier],
                apply=apply, sleep=sleep,
            )
            outcome.tier_changes.append((repo, old_tier, new_tier))

        sleep(2)

    return outcome
