"""Atlas membership check.

Builds the set of repos the atlas already holds from three places: the
systems pages of the atlas repo (one tarball download, frontmatter and
index parsed locally), forks under the archive org, and repos that already
have a scout issue in the target repo. The result is cached under state/
for 24 hours so a run refreshes it at most once.
"""

from __future__ import annotations

import io
import json
import re
import sys
import tarfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import requests

from .config import Config
from .models import normalize_repo
from .rate_limit import github_rate_limited, request_with_backoff

TARBALL_URL = "https://codeload.github.com/{repo}/tar.gz/refs/heads/{branch}"
CACHE_MAX_AGE_SECONDS = 24 * 3600

REASON_ATLAS = "already in the atlas"
REASON_ARCHIVE = "archived under agent-memory-atlas-archive"
REASON_ISSUE = "scout issue already filed"

_AZ_REPO_RE = re.compile(r'<code class="az-repo">([^<]+)</code>')
_CANDIDATE_TITLE_RE = re.compile(r"candidate:\s*([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)")


@dataclass
class AtlasSet:
    """Known repos plus why each one is known."""

    reasons: dict[str, str] = field(default_factory=dict)

    @property
    def repos(self) -> set[str]:
        return set(self.reasons)

    def is_known(self, repo: str) -> str | None:
        """Return the skip reason if repo is known, else None."""
        try:
            normalized = normalize_repo(repo)
        except ValueError:
            return None
        return self.reasons.get(normalized)

    def to_pairs(self) -> list[list[str]]:
        return sorted([repo, reason] for repo, reason in self.reasons.items())

    @classmethod
    def from_pairs(cls, pairs: list[list[str]]) -> "AtlasSet":
        return cls(reasons={repo: reason for repo, reason in pairs})


def parse_frontmatter(text: str) -> dict[str, str]:
    """Simple line parser for YAML frontmatter between --- markers."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    data: dict[str, str] = {}
    for line in lines[1:]:
        if line.strip() == "---":
            break
        key, sep, value = line.partition(":")
        if sep:
            data[key.strip()] = value.strip().strip('"').strip("'")
    return data


def parse_systems_index(text: str) -> set[str]:
    """Owner/repo names inside <code class="az-repo"> tags."""
    repos: set[str] = set()
    for match in _AZ_REPO_RE.finditer(text):
        try:
            repos.add(normalize_repo(match.group(1)))
        except ValueError:
            continue
    return repos


def parse_tarball(data: bytes) -> set[str]:
    """Repos named in content/systems/*.md frontmatter and the index page."""
    repos: set[str] = set()
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        for member in tar.getmembers():
            if not member.isfile():
                continue
            parts = Path(member.name).parts
            if len(parts) >= 3 and parts[-3:-1] == ("content", "systems"):
                text = tar.extractfile(member).read().decode("utf-8")
                frontmatter = parse_frontmatter(text)
                for key in ("source_name", "source_url"):
                    value = frontmatter.get(key)
                    if value:
                        try:
                            repos.add(normalize_repo(value))
                        except ValueError:
                            continue
            elif parts[-1] == "systems-index.md":
                text = tar.extractfile(member).read().decode("utf-8")
                repos |= parse_systems_index(text)
    return repos


def _headers(token: str | None) -> dict[str, str]:
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "scout"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _fetch_archive_org(
    session: requests.Session, org: str, headers: dict[str, str]
) -> set[str]:
    """Forks under the archive org, named <owner>--<repo>, mapped back."""
    repos: set[str] = set()
    page = 1
    while True:
        response = request_with_backoff(
            lambda: session.get(
                f"https://api.github.com/orgs/{org}/repos",
                params={"per_page": 100, "page": page},
                headers=headers,
                timeout=30,
            ),
            should_retry=github_rate_limited,
        )
        response.raise_for_status()
        items = response.json()
        for item in items:
            name = item.get("name", "")
            if "--" not in name:
                continue
            owner, repo = name.split("--", 1)
            try:
                repos.add(normalize_repo(f"{owner}/{repo}"))
            except ValueError:
                continue
        if len(items) < 100:
            return repos
        page += 1


def _fetch_filed_repos(
    session: requests.Session, config: Config, headers: dict[str, str]
) -> set[str]:
    """Repos with an existing scout issue in the target repo, open or closed."""
    query = (
        f'repo:{config.issues.target_repo} '
        f'label:"{config.issues.label}" in:title "candidate:"'
    )
    response = request_with_backoff(
        lambda: session.get(
            "https://api.github.com/search/issues",
            params={"q": query, "per_page": 100},
            headers=headers,
            timeout=30,
        ),
        should_retry=github_rate_limited,
    )
    response.raise_for_status()
    repos: set[str] = set()
    for item in response.json().get("items", []):
        match = _CANDIDATE_TITLE_RE.search(item.get("title", ""))
        if match:
            try:
                repos.add(normalize_repo(match.group(1)))
            except ValueError:
                continue
    return repos


def _cache_path(config: Config) -> Path:
    return config.state.db_path.parent / "atlas-cache.json"


def _read_cache(config: Config) -> AtlasSet | None:
    path = _cache_path(config)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
        fetched_at = datetime.fromisoformat(data["fetched_at"])
        if fetched_at.tzinfo is None:
            fetched_at = fetched_at.replace(tzinfo=UTC)
    except (OSError, ValueError, KeyError, TypeError):
        return None
    age = (datetime.now(UTC) - fetched_at).total_seconds()
    if age > CACHE_MAX_AGE_SECONDS:
        return None
    try:
        return AtlasSet.from_pairs(data["repos"])
    except (KeyError, TypeError, ValueError):
        return None


def _write_cache(config: Config, atlas: AtlasSet) -> None:
    path = _cache_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "fetched_at": datetime.now(UTC).isoformat(),
        "repos": atlas.to_pairs(),
    }
    path.write_text(json.dumps(payload, indent=2))


def _fetch_tarball(session: requests.Session, config: Config) -> bytes:
    url = TARBALL_URL.format(repo=config.atlas.repo, branch=config.atlas.branch)
    response = session.get(url, headers={"User-Agent": "scout"}, timeout=60)
    response.raise_for_status()
    return response.content


def load_atlas(
    config: Config,
    *,
    token: str | None = None,
    session: requests.Session | None = None,
    fetch_tarball=None,
) -> AtlasSet:
    """Build the set of known repos, refreshing the cache at most once a run."""
    cached = _read_cache(config)
    if cached is not None:
        return cached
    if session is None:
        session = requests.Session()
    if fetch_tarball is None:
        def fetch_tarball() -> bytes:
            return _fetch_tarball(session, config)

    headers = _headers(token)
    reasons: dict[str, str] = {}
    for repo in parse_tarball(fetch_tarball()):
        reasons.setdefault(repo, REASON_ATLAS)
    for repo in _fetch_archive_org(session, config.atlas.archive_org, headers):
        reasons.setdefault(repo, REASON_ARCHIVE)
    if token:
        for repo in _fetch_filed_repos(session, config, headers):
            reasons.setdefault(repo, REASON_ISSUE)
    else:
        print(
            "atlas: no token, skipping the scout issue check",
            file=sys.stderr,
        )
    atlas = AtlasSet(reasons=reasons)
    _write_cache(config, atlas)
    return atlas
