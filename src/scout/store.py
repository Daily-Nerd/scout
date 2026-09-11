"""Local SQLite state, optionally backed by durable candidate history.

Everything is idempotent on purpose: a rerun must never file the same repo
twice or refetch a post we already ingested. The optional JSONL history lets
that guarantee survive a fresh GitHub Actions runner.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from .history import History

_SCHEMA = """
CREATE TABLE IF NOT EXISTS seen_posts (
    post_id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    seen_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS seen_repos (
    repo TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    seen_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS filed_issues (
    repo TEXT PRIMARY KEY,
    issue_number INTEGER NOT NULL,
    filed_at TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now(UTC).isoformat()


class Store:
    def __init__(self, path: str | Path, history_path: str | Path | None = None):
        self.path = Path(path)
        self.history = History(history_path) if history_path is not None else None
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path)
        self._conn.executescript(_SCHEMA)

    def close(self) -> None:
        if self.history is not None:
            self.history.write()
        self._conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # posts ---------------------------------------------------------------

    def mark_post_seen(self, post_id: str, source: str) -> None:
        if self.history is not None:
            self.history.mark_post_seen(post_id, source)
        self._conn.execute(
            "INSERT OR IGNORE INTO seen_posts VALUES (?, ?, ?)",
            (post_id, source, _now()),
        )
        self._conn.commit()

    def is_post_seen(self, post_id: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM seen_posts WHERE post_id = ?", (post_id,)
        ).fetchone()
        return row is not None or (
            self.history is not None and self.history.is_post_seen(post_id)
        )

    # repos seen by a source -----------------------------------------------

    def mark_repo_seen(self, repo: str, source: str) -> None:
        if self.history is not None:
            self.history.mark_repo_seen(repo, source)
        self._conn.execute(
            "INSERT OR IGNORE INTO seen_repos VALUES (?, ?, ?)",
            (repo, source, _now()),
        )
        self._conn.commit()

    def is_repo_seen(self, repo: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM seen_repos WHERE repo = ?", (repo,)
        ).fetchone()
        return row is not None or (
            self.history is not None and self.history.is_repo_seen(repo)
        )

    # issues filed ----------------------------------------------------------

    def mark_filed(self, repo: str, issue_number: int) -> None:
        self._conn.execute(
            "INSERT OR IGNORE INTO filed_issues VALUES (?, ?, ?)",
            (repo, issue_number, _now()),
        )
        self._conn.commit()

    def is_filed(self, repo: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM filed_issues WHERE repo = ?", (repo,)
        ).fetchone()
        return row is not None

    def filed_issue_number(self, repo: str) -> int | None:
        row = self._conn.execute(
            "SELECT issue_number FROM filed_issues WHERE repo = ?", (repo,)
        ).fetchone()
        return row[0] if row else None

    def record_candidates(self, candidates: list) -> None:
        if self.history is not None:
            self.history.record_candidates(candidates)

    def mark_history_issue(self, repo: str, issue_number: int) -> None:
        if self.history is not None:
            self.history.mark_issue(repo, issue_number)

    def pending_candidates(self) -> list:
        return self.history.pending_candidates() if self.history is not None else []

    def readme_size(self, repo: str) -> int | None:
        if self.history is None:
            return None
        return self.history.readme_size(repo)

    def has_readme_size(self, repo: str) -> bool:
        return self.history is not None and self.history.has_readme_size(repo)

    def save_readme_size(self, repo: str, size: int | None) -> None:
        if self.history is not None:
            self.history.update_readme_size(repo, size)

    @property
    def issues_migrated(self) -> bool:
        return self.history is not None and self.history.issues_migrated

    def mark_issues_migrated(self) -> None:
        if self.history is not None:
            self.history.mark_issues_migrated()

    def mark_title_only_rows(self) -> int:
        if self.history is None:
            return 0
        return self.history.mark_title_only_rows()
