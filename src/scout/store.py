"""Local SQLite state: what we have seen and what we have filed.

Everything is idempotent on purpose: a rerun must never file the same
repo twice or refetch a post we already ingested.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from pathlib import Path

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
    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path)
        self._conn.executescript(_SCHEMA)

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    # posts ---------------------------------------------------------------

    def mark_post_seen(self, post_id: str, source: str) -> None:
        self._conn.execute(
            "INSERT OR IGNORE INTO seen_posts VALUES (?, ?, ?)",
            (post_id, source, _now()),
        )
        self._conn.commit()

    def is_post_seen(self, post_id: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM seen_posts WHERE post_id = ?", (post_id,)
        ).fetchone()
        return row is not None

    # repos seen by a source -----------------------------------------------

    def mark_repo_seen(self, repo: str, source: str) -> None:
        self._conn.execute(
            "INSERT OR IGNORE INTO seen_repos VALUES (?, ?, ?)",
            (repo, source, _now()),
        )
        self._conn.commit()

    def is_repo_seen(self, repo: str) -> bool:
        row = self._conn.execute(
            "SELECT 1 FROM seen_repos WHERE repo = ?", (repo,)
        ).fetchone()
        return row is not None

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
