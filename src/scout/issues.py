"""Issue filing: one GitHub issue per new candidate.

The body renderer is pure text and unit tested on its own. Filing is
idempotent on two levels: the local store of filed repos and a paginated
scan of existing issues in the target repo. Without --apply nothing is created.
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass

import requests

from .config import Config
from .github_search import TOKEN_ENV, TokenMissingError
from .models import Candidate, normalize_repo
from .rate_limit import github_rate_limited, request_with_backoff
from .store import Store
from .tiering import REASON_TIER_C, TierScore, component_summary

API = "https://api.github.com"
STATUS_FILED = "filed"
STATUS_SKIPPED = "skipped"
STATUS_DRY_RUN = "dry-run"
REASON_HELD = "held: over max_per_run"
LABEL_COLOR = "1d76db"
TIER_LABEL_COLORS = {"scout:tier-a": "0e8a16", "scout:tier-b": "1d76db"}

_CANDIDATE_TITLE_RE = re.compile(r"candidate:\s*([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)")


@dataclass
class FiledIssue:
    repo: str
    status: str  # filed, skipped or dry-run
    issue_number: int | None = None
    reason: str | None = None
    title: str = ""
    body: str = ""


def render_title(candidate: Candidate) -> str:
    return f"candidate: {candidate.repo}"


def render_body(candidate: Candidate, tier: TierScore | None = None) -> str:
    """Plain text issue body. Pure: no IO, no side effects.

    When a TierScore is passed, the body carries the tier and the score
    components so a curator can see why the candidate landed where it did.
    """
    lines = [
        f"Repo: {candidate.html_url or f'https://github.com/{candidate.repo}'}",
        f"Description: {candidate.description or 'none listed'}",
        f"Stars: {candidate.stars if candidate.stars is not None else 'unknown'}",
        f"Last push: {candidate.pushed_at or 'unknown'}",
        f"License: {candidate.license or 'none detected'}",
    ]
    if tier is not None:
        tier_line = f"Tier: {tier.tier.upper()} (score {tier.score:.2f})"
        if tier.absent:
            tier_line += f" (partial: {', '.join(tier.absent)})"
        lines.append(tier_line)
        lines.append(f"Score components: {component_summary(tier)}")
    if candidate.source == "reddit":
        author = f" by {candidate.author}" if candidate.author else ""
        lines.append(
            f"Source: Reddit post in r/{candidate.subreddit}{author}: "
            f"{candidate.source_url}"
        )
    else:
        query = candidate.matched_terms[0] if candidate.matched_terms else "unknown"
        lines.append(f"Source: GitHub search hit, matched query: {query}")
    terms = ", ".join(candidate.matched_terms) or "none recorded"
    lines.append(f"Matched terms: {terms}")
    lines.append("")
    lines.append(
        "Scout found this candidate; a person decides whether it enters "
        "the Agent Memory Atlas."
    )
    return "\n".join(lines)


def _headers(token: str | None) -> dict[str, str]:
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "scout"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def fetch_existing_issue_repos(
    session: requests.Session,
    config: Config,
    headers: dict[str, str],
    *,
    sleep=time.sleep,
) -> dict[str, int]:
    """Repo -> issue number for every candidate issue in the target repo."""
    found: dict[str, int] = {}
    for page in range(1, 101):
        response = request_with_backoff(
            lambda: session.get(
                f"{API}/repos/{config.issues.target_repo}/issues",
                params={"state": "all", "per_page": 100, "page": page},
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
            labels = {label.get("name") for label in item.get("labels", [])}
            if config.issues.label not in labels:
                continue
            match = _CANDIDATE_TITLE_RE.search(item.get("title", ""))
            if match:
                try:
                    found[normalize_repo(match.group(1))] = item["number"]
                except (ValueError, KeyError):
                    continue
        if len(items) < 100:
            return found
    return found


def migrate_existing_issues(
    config: Config,
    store: Store,
    token: str,
    *,
    session: requests.Session | None = None,
    sleep=time.sleep,
) -> int:
    """Import existing candidate issues once so a fresh runner starts clean."""
    if store.issues_migrated:
        return 0
    if session is None:
        session = requests.Session()
    found = fetch_existing_issue_repos(
        session, config, _headers(token), sleep=sleep
    )
    for repo, number in found.items():
        store.mark_filed(repo, number)
        store.mark_history_issue(repo, number)
    store.mark_issues_migrated()
    return len(found)


def _ensure_label(
    session: requests.Session,
    config: Config,
    headers: dict[str, str],
    name: str,
    color: str,
    *,
    sleep=time.sleep,
) -> None:
    labels_url = f"{API}/repos/{config.issues.target_repo}/labels"
    response = request_with_backoff(
        lambda: session.get(
            f"{labels_url}/{name}", headers=headers, timeout=30
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
            json={"name": name, "color": color},
            timeout=30,
        ),
        should_retry=github_rate_limited,
        sleep=sleep,
        max_retries=config.issues.max_rate_limit_retries,
    )
    create.raise_for_status()


def file_candidates(
    config: Config,
    store: Store,
    candidates: list[Candidate],
    *,
    apply: bool,
    token: str | None,
    session: requests.Session | None = None,
    sleep=time.sleep,
    tiers: dict[str, TierScore] | None = None,
    existing: dict[str, int] | None = None,
) -> list[FiledIssue]:
    """Decide and (with apply) create one issue per candidate.

    Repos already in the store or found by title search are skipped, so the
    same repo is never filed twice. Candidates scored tier C are not filed
    at all: they stay pending in the history for a later run. When
    ``existing`` (repo -> issue number) is passed it is trusted as the
    target-repo scan and no issue walk is done here; callers that also
    need the map for the atlas check should fetch it once and share it.

    Once tier-C and already-filed candidates are out, the remainder is
    ranked by score (highest first, ties broken by repo name) and only
    ``config.filing.max_per_run`` of them are filed; the rest stay pending
    with reason REASON_HELD. This holds in dry runs too, so a dry run
    prints exactly what an apply run would file.
    """
    if apply and not token:
        raise TokenMissingError(
            f"{TOKEN_ENV} is required to file issues; "
            "set it in the environment or in .env"
        )
    if session is None:
        session = requests.Session()
    headers = _headers(token)

    pending = [c for c in candidates if not store.is_filed(c.repo)]
    if pending and existing is None:
        existing = fetch_existing_issue_repos(session, config, headers, sleep=sleep)
    if existing:
        for repo, number in existing.items():
            store.mark_filed(repo, number)

    def _score(candidate: Candidate) -> float:
        tier = tiers.get(candidate.repo) if tiers else None
        return tier.score if tier is not None else 0.0

    eligible = [
        candidate
        for candidate in candidates
        if not (tiers and tiers.get(candidate.repo) and tiers[candidate.repo].tier == "c")
        and not store.is_filed(candidate.repo)
    ]
    ranked = sorted(eligible, key=lambda candidate: (-_score(candidate), candidate.repo))
    held_repos = {candidate.repo for candidate in ranked[config.filing.max_per_run:]}

    results: list[FiledIssue] = []
    labels_ready: set[str] = set()
    last_issue_at = 0.0
    for candidate in candidates:
        tier = tiers.get(candidate.repo) if tiers else None
        if tier is not None and tier.tier == "c":
            results.append(
                FiledIssue(
                    repo=candidate.repo,
                    status=STATUS_SKIPPED,
                    reason=REASON_TIER_C,
                )
            )
            continue
        if store.is_filed(candidate.repo):
            results.append(
                FiledIssue(
                    repo=candidate.repo,
                    status=STATUS_SKIPPED,
                    issue_number=store.filed_issue_number(candidate.repo),
                    reason="issue already filed",
                )
            )
            continue
        if candidate.repo in held_repos:
            results.append(
                FiledIssue(
                    repo=candidate.repo,
                    status=STATUS_SKIPPED,
                    reason=REASON_HELD,
                )
            )
            continue
        title = render_title(candidate)
        body = render_body(candidate, tier)
        labels = [config.issues.label]
        if tier is not None:
            labels.append(tier.label)
        if not apply:
            results.append(
                FiledIssue(
                    repo=candidate.repo,
                    status=STATUS_DRY_RUN,
                    title=title,
                    body=body,
                )
            )
            continue
        for label in labels:
            if label not in labels_ready:
                color = TIER_LABEL_COLORS.get(label, LABEL_COLOR)
                _ensure_label(session, config, headers, label, color, sleep=sleep)
                labels_ready.add(label)
        wait = config.issues.request_interval_seconds - (
            time.monotonic() - last_issue_at
        )
        if wait > 0:
            sleep(wait)
        response = request_with_backoff(
            lambda: session.post(
                f"{API}/repos/{config.issues.target_repo}/issues",
                headers=headers,
                json={
                    "title": title,
                    "body": body,
                    "labels": labels,
                },
                timeout=30,
            ),
            should_retry=github_rate_limited,
            sleep=sleep,
            max_retries=config.issues.max_rate_limit_retries,
        )
        last_issue_at = time.monotonic()
        response.raise_for_status()
        number = response.json()["number"]
        store.mark_filed(candidate.repo, number)
        results.append(
            FiledIssue(
                repo=candidate.repo,
                status=STATUS_FILED,
                issue_number=number,
                title=title,
                body=body,
            )
        )
    return results
