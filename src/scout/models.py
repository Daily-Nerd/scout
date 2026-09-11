"""Shared data shapes and repo-name normalisation."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

_HOST_RE = re.compile(r"^(?:https?://)?(?:www\.)?github\.com/", re.IGNORECASE)
_TRAILING_RE = re.compile(r"(?:\.git)?/?$")


def normalize_repo(name: str) -> str:
    """Return owner/repo, lowercase, with scheme, host, .git and trailing
    slash stripped."""
    name = _HOST_RE.sub("", name.strip())
    name = _TRAILING_RE.sub("", name)
    parts = [p for p in name.split("/") if p]
    if len(parts) < 2:
        raise ValueError(f"not an owner/repo name: {name!r}")
    return "/".join(parts[:2]).lower()


@dataclass
class Candidate:
    """A repo a source thinks might belong in the atlas."""

    repo: str  # normalised owner/repo
    source: str  # "github" or "reddit"
    source_url: str  # search hit url, or post permalink
    matched_terms: list[str] = field(default_factory=list)
    description: str = ""
    topics: list[str] = field(default_factory=list)
    stars: int | None = None
    pushed_at: str | None = None
    license: str | None = None
    html_url: str | None = None
    subreddit: str | None = None
    author: str | None = None
    posted_at: str | None = None
