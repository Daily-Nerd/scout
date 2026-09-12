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

    def gate(self, text: str) -> list[str] | None:
        """Terms matched in text, or None unless at least one agent-ish and
        one memory-ish term both appear (case-insensitive)."""
        lowered = text.lower()
        agentish = [t for t in self.agentish if t.lower() in lowered]
        memoryish = [t for t in self.memoryish if t.lower() in lowered]
        if not agentish or not memoryish:
            return None
        return sorted(agentish + memoryish)


@dataclass
class AtlasConfig:
    repo: str = "neoneye/agent-memory-atlas"
    branch: str = "main"
    archive_org: str = "agent-memory-atlas-archive"


@dataclass
class IssuesConfig:
    target_repo: str = "Daily-Nerd/scout"
    label: str = "scout:candidate"
    request_interval_seconds: float = 2.0
    max_rate_limit_retries: int = 3


_DEFAULT_SOURCE_EXTENSIONS = [
    ".py", ".ts", ".tsx", ".js", ".go", ".rs", ".java", ".kt",
    ".rb", ".cs", ".cpp", ".c", ".swift",
]
_DEFAULT_LIST_WORDS = [
    "awesome", "list", "curated", "collection", "resources", "roundup",
]


@dataclass
class TieringConfig:
    stars_weight: float = 4.0
    stars_cap: int = 500
    recency_weight: float = 2.0
    recency_window_days: int = 90
    name_weight: float = 2.0
    readme_weight: float = 2.0
    readme_cap_bytes: int = 20000
    topic_weight: float = 1.0
    density_weight: float = 3.0
    tier_a_min: float = 10.0
    tier_b_min: float = 7.0
    tests_weight: float = 2.0
    source_weight: float = 1.5
    source_cap: int = 40
    source_extensions: list[str] = field(
        default_factory=lambda: list(_DEFAULT_SOURCE_EXTENSIONS)
    )
    list_penalty_weight: float = 3.0
    list_words: list[str] = field(default_factory=lambda: list(_DEFAULT_LIST_WORDS))
    tree_max_per_run: int = 300


@dataclass
class FilingConfig:
    max_per_run: int = 25


@dataclass
class RefreshConfig:
    days: int = 14
    max_per_run: int = 50


@dataclass
class StateConfig:
    db_path: Path = Path("state/scout.db")
    candidates_path: Path = Path("data/candidates.jsonl")
    reports_path: Path = Path("reports")


@dataclass
class Config:
    github: GitHubConfig
    reddit: RedditConfig
    terms: TermConfig
    atlas: AtlasConfig
    issues: IssuesConfig
    tiering: TieringConfig
    filing: FilingConfig
    refresh: RefreshConfig
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
    tiering = _section(data, "tiering")
    filing = _section(data, "filing")
    refresh = _section(data, "refresh")
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
            request_interval_seconds=float(issues.get("request_interval_seconds", 2)),
            max_rate_limit_retries=int(issues.get("max_rate_limit_retries", 3)),
        ),
        tiering=TieringConfig(
            stars_weight=float(tiering.get("stars_weight", 4)),
            stars_cap=int(tiering.get("stars_cap", 500)),
            recency_weight=float(tiering.get("recency_weight", 2)),
            recency_window_days=int(tiering.get("recency_window_days", 90)),
            name_weight=float(tiering.get("name_weight", 2)),
            readme_weight=float(tiering.get("readme_weight", 2)),
            readme_cap_bytes=int(tiering.get("readme_cap_bytes", 20000)),
            topic_weight=float(tiering.get("topic_weight", 1)),
            density_weight=float(tiering.get("density_weight", 3)),
            tier_a_min=float(tiering.get("tier_a_min", 10)),
            tier_b_min=float(tiering.get("tier_b_min", 7)),
            tests_weight=float(tiering.get("tests_weight", 2)),
            source_weight=float(tiering.get("source_weight", 1.5)),
            source_cap=int(tiering.get("source_cap", 40)),
            source_extensions=list(
                tiering.get("source_extensions", _DEFAULT_SOURCE_EXTENSIONS)
            ),
            list_penalty_weight=float(tiering.get("list_penalty_weight", 3)),
            list_words=list(tiering.get("list_words", _DEFAULT_LIST_WORDS)),
            tree_max_per_run=int(tiering.get("tree_max_per_run", 300)),
        ),
        filing=FilingConfig(
            max_per_run=int(filing.get("max_per_run", 25)),
        ),
        refresh=RefreshConfig(
            days=int(refresh.get("days", 14)),
            max_per_run=int(refresh.get("max_per_run", 50)),
        ),
        state=StateConfig(
            db_path=Path(state.get("db_path", "state/scout.db")),
            candidates_path=Path(
                state.get("candidates_path", "data/candidates.jsonl")
            ),
            reports_path=Path(state.get("reports_path", "reports")),
        ),
        path=path,
    )
