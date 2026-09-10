"""Issue filing: one GitHub issue per new candidate.

The body renderer is pure text and unit tested on its own. Filing is
idempotent on two levels: the local store of filed repos and a title search
of existing issues in the target repo. Without --apply nothing is created.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

import requests

from .config import Config
from .github_search import TOKEN_ENV, TokenMissingError
from .models import Candidate, normalize_repo
from .store import Store

API = "https://api.github.com"
STATUS_FILED = "filed"
STATUS_SKIPPED = "skipped"
STATUS_DRY_RUN = "dry-run"
LABEL_COLOR = "1d76db"

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


def render_body(candidate: Candidate) -> str:
    """Plain text issue body. Pure: no IO, no side effects."""
    lines = [
        f"Repo: {candidate.html_url or f'https://github.com/{candidate.repo}'}",
        f"Description: {candidate.description or 'none listed'}",
        f"Stars: {candidate.stars if candidate.stars is not None else 'unknown'}",
        f"Last push: {candidate.pushed_at or 'unknown'}",
        f"License: {candidate.license or 'none detected'}",
    ]
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


def _existing_issue_repos(
    session: requests.Session, config: Config, headers: dict[str, str]
) -> dict[str, int]:
    """Repo -> issue number for every candidate issue in the target repo."""
    query = (
        f'repo:{config.issues.target_repo} '
        f'label:"{config.issues.label}" in:title "candidate:"'
    )
    response = session.get(
        f"{API}/search/issues",
        params={"q": query, "per_page": 100},
        headers=headers,
        timeout=30,
    )
    response.raise_for_status()
    found: dict[str, int] = {}
    for item in response.json().get("items", []):
        match = _CANDIDATE_TITLE_RE.search(item.get("title", ""))
        if match:
            try:
                found[normalize_repo(match.group(1))] = item["number"]
            except (ValueError, KeyError):
                continue
    return found


def _ensure_label(
    session: requests.Session, config: Config, headers: dict[str, str]
) -> None:
    labels_url = f"{API}/repos/{config.issues.target_repo}/labels"
    response = session.get(
        f"{labels_url}/{config.issues.label}", headers=headers, timeout=30
    )
    if response.status_code == 200:
        return
    if response.status_code != 404:
        response.raise_for_status()
    create = session.post(
        labels_url,
        headers=headers,
        json={"name": config.issues.label, "color": LABEL_COLOR},
        timeout=30,
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
) -> list[FiledIssue]:
    """Decide and (with apply) create one issue per candidate.

    Repos already in the store or found by title search are skipped, so the
    same repo is never filed twice.
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
    if pending:
        for repo, number in _existing_issue_repos(session, config, headers).items():
            store.mark_filed(repo, number)

    results: list[FiledIssue] = []
    label_ready = False
    for candidate in candidates:
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
        title = render_title(candidate)
        body = render_body(candidate)
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
        if not label_ready:
            _ensure_label(session, config, headers)
            label_ready = True
        response = session.post(
            f"{API}/repos/{config.issues.target_repo}/issues",
            headers=headers,
            json={"title": title, "body": body, "labels": [config.issues.label]},
            timeout=30,
        )
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
