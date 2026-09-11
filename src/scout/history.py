"""Versioned candidate history shared across scheduled runs."""

from __future__ import annotations

import json
from dataclasses import asdict, fields
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING

from .models import Candidate

if TYPE_CHECKING:
    from .tiering import TierScore

_REFRESH_DISCOVERY = ("seen", "filed", "title_only")


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _parse_iso(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


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
        field_names = {field.name for field in fields(Candidate)}
        for row in self.repos.values():
            if row.get("status", "pending") != "pending" or row.get("issue_number"):
                continue
            latest = row.get("latest")
            if not isinstance(latest, dict):
                continue
            payload = {key: value for key, value in latest.items()
                       if key in field_names}
            try:
                pending.append(Candidate(**payload))
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
                "discovery": "seen",
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
        row["discovery"] = "filed"

    def record_score(self, repo: str, score: TierScore) -> None:
        """Persist a TierScore onto the repo's row.

        Score and each component value are rounded to 4 decimals. A
        component's input is stored as-is (lists stay lists) since it must
        already be JSON-serializable by the time it reaches a TierScore.
        """
        row = self.repos.setdefault(
            repo,
            {"kind": "repo", "repo": repo, "first_seen_at": _now(), "sources": []},
        )
        row["score"] = round(score.score, 4)
        row["tier"] = score.tier
        row["components"] = {
            name: {
                "input": component.input,
                "value": None if component.value is None else round(component.value, 4),
            }
            for name, component in score.components.items()
        }
        row["absent_components"] = list(score.absent)
        row["scored_at"] = score.scored_at
        row["assessment"] = f"tier-{score.tier}"

    def mark_known(self, repo: str) -> None:
        """Flag a repo the atlas already knows about. Known repos are not scored."""
        row = self.repos.setdefault(
            repo,
            {"kind": "repo", "repo": repo, "first_seen_at": _now(), "sources": []},
        )
        row["assessment"] = "atlas-known"

    def mark_retracted(self, repo: str) -> None:
        row = self.repos.setdefault(
            repo,
            {"kind": "repo", "repo": repo, "first_seen_at": _now(), "sources": []},
        )
        row["discovery"] = "retracted"

    def repo_for_issue(self, issue_number: int) -> str | None:
        """Reverse lookup: the repo whose row carries this issue number."""
        for repo, row in self.repos.items():
            if row.get("issue_number") == issue_number:
                return repo
        return None

    def mark_issues_migrated(self) -> None:
        self.issues_migrated = True

    def readme_size(self, repo: str) -> int | None:
        """Cached README size from the latest payload, or None if unknown."""
        row = self.repos.get(repo)
        latest = row.get("latest") if row else None
        if isinstance(latest, dict):
            value = latest.get("readme_bytes")
            if isinstance(value, int):
                return value
        return None

    def has_readme_size(self, repo: str) -> bool:
        """True once the README size was fetched, even if there is no README."""
        row = self.repos.get(repo)
        latest = row.get("latest") if row else None
        return isinstance(latest, dict) and "readme_bytes" in latest

    def update_readme_size(self, repo: str, size: int | None) -> None:
        row = self.repos.get(repo)
        latest = row.get("latest") if row else None
        if isinstance(latest, dict):
            latest["readme_bytes"] = size

    def tree_signals(self, repo: str) -> tuple[bool, int] | None:
        """Cached (has_tests, source_files) from the latest payload, or None
        if the git tree has not been fetched for this repo yet."""
        row = self.repos.get(repo)
        latest = row.get("latest") if row else None
        if isinstance(latest, dict) and "tree_tests" in latest:
            return bool(latest["tree_tests"]), int(latest.get("tree_source_files", 0))
        return None

    def has_tree_signals(self, repo: str) -> bool:
        """True once the git tree was fetched, even for an empty repo."""
        row = self.repos.get(repo)
        latest = row.get("latest") if row else None
        return isinstance(latest, dict) and "tree_tests" in latest

    def update_tree_signals(
        self, repo: str, has_tests: bool, source_files: int, fetched_at: str
    ) -> None:
        row = self.repos.get(repo)
        latest = row.get("latest") if row else None
        if isinstance(latest, dict):
            latest["tree_tests"] = has_tests
            latest["tree_source_files"] = source_files
            latest["tree_fetched_at"] = fetched_at

    @staticmethod
    def _refresh_freshness_stamp(row: dict) -> str:
        """The later of scored_at and refresh_attempted_at, or "" if
        neither is set. Both are written in the same zero-padded
        "%Y-%m-%dT%H:%M:%SZ" shape, so the plain string max is also the
        chronological max. A failed attempt (404, upstream error, or a
        label patch that did not go through) still counts: it must not
        make a dead row sort first forever."""
        return max(row.get("scored_at") or "", row.get("refresh_attempted_at") or "")

    def rows_due_for_refresh(self, now: datetime, days: int, limit: int) -> list[str]:
        """Repo rows overdue for a metadata refresh, oldest first.

        A row is due when its discovery is seen, filed or title_only, its
        assessment is not atlas-known, and its freshness stamp (the later
        of scored_at and refresh_attempted_at) is absent or older than
        `days`. An unparseable stamp counts as due, same as an absent
        one. Results are ordered by that stamp then last_seen_at, both
        ascending (a missing value sorts first), and capped at `limit`.
        """
        cutoff = now - timedelta(days=days)
        due: list[str] = []
        for repo, row in self.repos.items():
            if row.get("kind") != "repo":
                continue
            if row.get("discovery") not in _REFRESH_DISCOVERY:
                continue
            if row.get("assessment") == "atlas-known":
                continue
            stamp = self._refresh_freshness_stamp(row)
            if stamp:
                parsed = _parse_iso(stamp)
                if parsed is not None and parsed >= cutoff:
                    continue
            due.append(repo)
        due.sort(key=lambda repo: (
            self._refresh_freshness_stamp(self.repos[repo]),
            self.repos[repo].get("last_seen_at") or "",
        ))
        return due[:limit]

    def mark_refresh_attempt(
        self, repo: str, attempted_at: str, *, error: str | None = None
    ) -> None:
        """Stamp a row with the outcome of one refresh attempt.

        `refresh_attempted_at` is set whether the attempt succeeded or
        failed, so a permanently-404ing repo still advances its
        freshness stamp instead of starving the queue by always sorting
        first. `refresh_error` (a short string such as "404", "500" or
        "label patch failed") is set on failure and removed entirely on
        success, so a clean row carries no error key at all.
        """
        row = self.repos.setdefault(
            repo,
            {"kind": "repo", "repo": repo, "first_seen_at": _now(), "sources": []},
        )
        row["refresh_attempted_at"] = attempted_at
        if error:
            row["refresh_error"] = error
        else:
            row.pop("refresh_error", None)

    def update_latest(self, repo: str, **payload: object) -> None:
        """Merge fields into a row's latest payload, creating it first if
        the row has none yet (a title_only row rebuilt from an issue
        title, or one seen before latest was recorded)."""
        row = self.repos.setdefault(
            repo,
            {"kind": "repo", "repo": repo, "first_seen_at": _now(), "sources": []},
        )
        latest = row.get("latest")
        if not isinstance(latest, dict):
            latest = {"repo": repo}
            row["latest"] = latest
        latest.update(payload)

    def mark_title_only_rows(self) -> int:
        """Flag repo rows that carry no candidate payload.

        Rows rebuilt from issue titles alone hold no stars, description or
        source data, so they never count as records with data. A repo row
        written by a normal scan always has a latest payload.
        """
        marked = 0
        for row in self.repos.values():
            if not isinstance(row.get("latest"), dict) and not row.get("title_only"):
                row["title_only"] = True
                if not row.get("issue_number"):
                    row["discovery"] = "title_only"
                marked += 1
        return marked

    def repair_rows(self, retracted_through: int | None = None) -> dict[str, int]:
        """Backfill discovery and assessment on rows written before those
        fields existed, and fix the readme_bytes 404-as-null cache bug.

        Never touches a field a row already carries; existing data always
        wins over a guess. Returns how many rows changed per field.
        """
        counts = {"discovery": 0, "assessment": 0, "readme_bytes": 0}
        for row in self.repos.values():
            if row.get("kind") != "repo":
                continue
            if "discovery" not in row:
                # An issue number outranks the title_only flag: every row
                # filed before the retraction was also rebuilt from its
                # issue title, and the flag only says where the row came
                # from, not what scout did with it.
                issue_number = row.get("issue_number")
                if issue_number and retracted_through is not None \
                        and issue_number <= retracted_through:
                    row["discovery"] = "retracted"
                elif issue_number:
                    row["discovery"] = "filed"
                elif row.get("title_only"):
                    row["discovery"] = "title_only"
                else:
                    row["discovery"] = "seen"
                counts["discovery"] += 1
            if "assessment" not in row:
                row["assessment"] = "none"
                counts["assessment"] += 1
            latest = row.get("latest")
            if isinstance(latest, dict) and "readme_bytes" in latest \
                    and latest["readme_bytes"] is None:
                latest["readme_bytes"] = 0
                counts["readme_bytes"] += 1
        return counts

    def write(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        rows = [*self.repos.values(), *self.posts.values()]
        if self.issues_migrated:
            rows.append({"kind": "meta", "key": "issues_migrated", "value": True})
        text = "".join(json.dumps(row, sort_keys=True) + "\n" for row in rows)
        temporary = self.path.with_suffix(self.path.suffix + ".tmp")
        temporary.write_text(text)
        temporary.replace(self.path)
