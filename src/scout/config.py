"""Load scout.toml into typed config."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class GitHubConfig:
    queries: list[str] = field(default_factory=list)
    window_days: int = 14


@dataclass
class RedditConfig:
    subreddits: list[str] = field(default_factory=list)
    request_interval_seconds: float = 2.0
    max_rate_limit_retries: int = 3
    contact_url: str = ""


@dataclass
class TermConfig:
    agentish: list[str] = field(default_factory=list)
    memoryish: list[str] = field(default_factory=list)

    def match(self, text: str) -> list[str]:
        """Terms from either list found in text (case-insensitive), deduped."""
        lowered = text.lower()
        found = {
            term
            for term in (*self.agentish, *self.memoryish)
            if term.lower() in lowered
        }
        return sorted(found)


@dataclass
class AtlasConfig:
    repo: str = "neoneye/agent-memory-atlas"
    branch: str = "main"
    archive_org: str = "agent-memory-atlas-archive"


@dataclass
class IssuesConfig:
    target_repo: str = "Daily-Nerd/scout"
    label: str = "scout:candidate"
    request_interval_seconds: float = 1.0
    max_rate_limit_retries: int = 3


@dataclass
class StateConfig:
    db_path: Path = Path("state/scout.db")


@dataclass
class Config:
    github: GitHubConfig
    reddit: RedditConfig
    terms: TermConfig
    atlas: AtlasConfig
    issues: IssuesConfig
    state: StateConfig
    path: Path

    def reddit_user_agent(self) -> str:
        return f"scout (+{self.reddit.contact_url})" if self.reddit.contact_url else "scout"


def _section(data: dict, name: str) -> dict:
    section = data.get(name, {})
    if not isinstance(section, dict):
        raise ValueError(f"[{name}] must be a table")
    return section


def load(path: str | Path) -> Config:
    path = Path(path)
    with path.open("rb") as fh:
        data = tomllib.load(fh)

    gh = _section(data, "github")
    reddit = _section(data, "reddit")
    terms = _section(data, "terms")
    atlas = _section(data, "atlas")
    issues = _section(data, "issues")
    state = _section(data, "state")

    return Config(
        github=GitHubConfig(
            queries=list(gh.get("queries", [])),
            window_days=int(gh.get("window_days", 14)),
        ),
        reddit=RedditConfig(
            subreddits=list(reddit.get("subreddits", [])),
            request_interval_seconds=float(reddit.get("request_interval_seconds", 2)),
            max_rate_limit_retries=int(reddit.get("max_rate_limit_retries", 3)),
            contact_url=str(reddit.get("contact_url", "")),
        ),
        terms=TermConfig(
            agentish=list(terms.get("agentish", [])),
            memoryish=list(terms.get("memoryish", [])),
        ),
        atlas=AtlasConfig(
            repo=str(atlas.get("repo", "neoneye/agent-memory-atlas")),
            branch=str(atlas.get("branch", "main")),
            archive_org=str(atlas.get("archive_org", "agent-memory-atlas-archive")),
        ),
        issues=IssuesConfig(
            target_repo=str(issues.get("target_repo", "Daily-Nerd/scout")),
            label=str(issues.get("label", "scout:candidate")),
            request_interval_seconds=float(issues.get("request_interval_seconds", 1)),
            max_rate_limit_retries=int(issues.get("max_rate_limit_retries", 3)),
        ),
        state=StateConfig(db_path=Path(state.get("db_path", "state/scout.db"))),
        path=path,
    )
