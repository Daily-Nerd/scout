"""Versioned candidate history shared across scheduled runs."""

from __future__ import annotations

import json
from dataclasses import asdict
from datetime import UTC, datetime
from pathlib import Path

from .models import Candidate


def _now() -> str:
    return datetime.now(UTC).isoformat()


class History:
    """JSONL-backed index for durable discovery state and provenance."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.repos: dict[str, dict] = {}
        self.posts: dict[str, dict] = {}
        self.issues_migrated = False
        self._load()

    def _load(self) -> None:
        if not self.path.exists():
            return
        try:
            lines = self.path.read_text().splitlines()
        except OSError:
            return
        for line in lines:
            try:
                row = json.loads(line)
            except (json.JSONDecodeError, TypeError):
                continue
            if row.get("kind") == "repo" and row.get("repo"):
                self.repos[row["repo"]] = row
            elif row.get("kind") == "post" and row.get("post_id"):
                self.posts[row["post_id"]] = row
            elif row.get("kind") == "meta" and row.get("key") == "issues_migrated":
                self.issues_migrated = bool(row.get("value"))

    def is_repo_seen(self, repo: str) -> bool:
        return repo in self.repos

    def is_post_seen(self, post_id: str) -> bool:
        return post_id in self.posts

    def pending_candidates(self) -> list[Candidate]:
        pending: list[Candidate] = []
        for row in self.repos.values():
            if row.get("status", "pending") != "pending" or row.get("issue_number"):
                continue
            latest = row.get("latest")
            if not isinstance(latest, dict):
                continue
            try:
                pending.append(Candidate(**latest))
            except TypeError:
                continue
        return pending

    def mark_repo_seen(self, repo: str, source: str) -> None:
        now = _now()
        row = self.repos.setdefault(
            repo,
            {
                "kind": "repo",
                "repo": repo,
                "first_seen_at": now,
                "sources": [],
                "status": "pending",
            },
        )
        row["last_seen_at"] = now
        if source not in row.setdefault("sources", []):
            row["sources"].append(source)

    def mark_post_seen(self, post_id: str, source: str) -> None:
        self.posts.setdefault(
            post_id,
            {"kind": "post", "post_id": post_id, "source": source, "seen_at": _now()},
        )

    def record_candidates(self, candidates: list[Candidate]) -> None:
        for candidate in candidates:
            self.mark_repo_seen(candidate.repo, candidate.source)
            row = self.repos[candidate.repo]
            row["latest"] = asdict(candidate)
            source_urls = row.setdefault("source_urls", [])
            if candidate.source_url and candidate.source_url not in source_urls:
                source_urls.append(candidate.source_url)

    def mark_issue(self, repo: str, issue_number: int) -> None:
        row = self.repos.setdefault(
            repo,
            {"kind": "repo", "repo": repo, "first_seen_at": _now(), "sources": []},
        )
        row["issue_number"] = issue_number
        row["status"] = "filed"

    def mark_issues_migrated(self) -> None:
        self.issues_migrated = True

    def write(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        rows = [*self.repos.values(), *self.posts.values()]
        if self.issues_migrated:
            rows.append({"kind": "meta", "key": "issues_migrated", "value": True})
        text = "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(text)
        temporary.replace(self.path)
