"""Metadata refresh pass.

Rows discovered a while ago accumulate stale metadata: a Reddit hit never
had stars or pushed_at to begin with, and a GitHub hit's stars, recency
and topics only reflect the moment it was first seen. Each run, at most
config.refresh.max_per_run rows whose metadata was never fetched, or was
last fetched (by search or by refresh) more than config.refresh.days ago,
get a fresh GET /repos/{repo}, a forced (cache bypassing) tree and README
fetch, and a rescore. Staleness is measured from that last fetch, never
from scored_at: scoring runs every run from the cached payload. The tier a rescore
lands on can differ from what is on file, and when the row is already
filed, that issue's tier label is swapped to match.

Refresh never files an issue and never touches an issue beyond that one
label swap. A single row's upstream error, at any step, never aborts
the pass: it is recorded and the next row is still attempted.
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

    Each due row gets a fresh GET /repos/{repo}. Any non-2xx response
    (a 404, a 500, a 451, or a rate limit that outlives the backoff
    retries) is recorded in `failed`, stamped via
    `store.mark_refresh_attempt` with the status code as `error`, and
    the row is left exactly as it was: no further fetch, no rescore.
    This never raises, so one bad repo never aborts the rest of the
    pass and never crashes the run that called refresh().

    Otherwise `latest` is updated with the fresh stars, pushed_at,
    description, topics, license and html_url, and stamped with this
    run's `now` as `fetched_at`; the row's source is
    left untouched (a title_only row, which has no latest yet, has its
    source and source_url filled in instead, from the row's sources
    and the fetched html_url). The git tree and README are refetched
    unconditionally, bypassing the usual cache; either can come back
    unknown (None) on its own error without failing the row, the same
    way a normal scan tolerates a missing tree or README. The row is
    then rescored and re-recorded, and this always counts as
    refreshed, whether or not anything below it fails.

    When the resulting tier differs from what was on file, that is
    recorded in `tier_changes` regardless of whether the row has a
    filed issue. Only when it does is `issues.update_tier_label` also
    called to swap the issue's label; if that call fails with a
    requests error (a bad GET or PATCH), the failure is caught, recorded
    in `failed` with a "label patch failed" error, and the pass
    continues to the next row. Either way, every row visited gets exactly one
    `mark_refresh_attempt` stamp with the same `now`, so a row that
    keeps failing still advances its freshness stamp instead of
    starving the queue forever.
    """
    if store.history is None:
        return RefreshOutcome()
    if session is None:
        session = requests.Session()
    now = now or datetime.now(UTC)
    attempted_at = now.strftime("%Y-%m-%dT%H:%M:%SZ")
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
        if not (200 <= response.status_code < 300):
            outcome.failed.append(repo)
            store.mark_refresh_attempt(
                repo, attempted_at, error=str(response.status_code)
            )
            sleep(2)
            continue
        payload = response.json()

        extra: dict[str, object] = {}
        if not isinstance(row.get("latest"), dict):
            sources = row.get("sources") or []
            extra["source"] = sources[0] if sources else "github"
            extra["source_url"] = payload.get("html_url") or f"https://github.com/{repo}"

        license_info = payload.get("license") or {}
        store.update_latest(
            repo,
            fetched_at=attempted_at,
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
            store.save_tree_signals(repo, has_tests, source_files, attempted_at)
        else:
            has_tests, source_files = None, None

        readme_bytes = _fetch_readme_size(session, repo, headers, sleep=sleep)
        if readme_bytes is not None:
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

        new_tier = score.tier
        tier_changed = bool(old_tier) and old_tier != new_tier
        if tier_changed:
            outcome.tier_changes.append((repo, old_tier, new_tier))

        issue_number = row.get("issue_number")
        attempt_error: str | None = None
        if tier_changed and issue_number:
            # The label lookups sit outside the try: a tier with no label
            # is a programming error and must surface, only the HTTP
            # round trip is tolerated per row.
            old_label, new_label = TIER_LABELS[old_tier], TIER_LABELS[new_tier]
            try:
                issues_mod.update_tier_label(
                    config, session, token, issue_number, old_label, new_label,
                    apply=apply, sleep=sleep,
                )
            except requests.RequestException:
                attempt_error = "label patch failed"
                outcome.failed.append(repo)

        store.mark_refresh_attempt(repo, attempted_at, error=attempt_error)
        sleep(2)

    return outcome
