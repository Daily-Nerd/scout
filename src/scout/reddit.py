"""Reddit source over public RSS, anonymous by design.

One request per subreddit per run, spaced by config.reddit.request_interval_seconds,
with a descriptive User-Agent. On 429 or 403 the source retries a bounded number
of times using Retry-After or exponential backoff, then returns what it collected.
Seen post ids are persisted in the local store so a rerun only ingests new posts.
"""

from __future__ import annotations

import html
import re
import sys
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass

import requests

from .config import Config
from .models import Candidate, normalize_repo
from .rate_limit import request_with_backoff
from .store import Store

BACKOFF_STATUSES = {403, 429}
_REPO_URL_RE = re.compile(
    r"github\.com/([A-Za-z0-9][A-Za-z0-9.-]*)/([A-Za-z0-9][A-Za-z0-9._-]*)"
)
_TAG_RE = re.compile(r"<[^>]+>")
_TRAILING_PUNCT = ".,;:!?)]}'\"”’"


@dataclass
class Entry:
    post_id: str
    title: str
    link: str
    author: str | None
    updated: str | None
    content: str


def _strip_tags(markup: str) -> str:
    text = _TAG_RE.sub(" ", markup)
    return " ".join(html.unescape(text).split())


def _parse_atom(root: ET.Element) -> list[Entry]:
    entries = []
    for node in root.findall("{*}entry"):
        link_node = node.find("{*}link")
        link = (link_node.get("href") if link_node is not None else "") or ""
        title = node.findtext("{*}title") or ""
        post_id = node.findtext("{*}id") or link
        author = node.findtext("{*}author/{*}name")
        content = (
            node.findtext("{*}content")
            or node.findtext("{*}summary")
            or ""
        )
        entries.append(
            Entry(
                post_id=post_id.strip(),
                title=title.strip(),
                link=link.strip(),
                author=author.strip() if author else None,
                updated=(node.findtext("{*}updated") or "").strip() or None,
                content=content,
            )
        )
    return entries


def _parse_rss(root: ET.Element) -> list[Entry]:
    entries = []
    for node in root.findall(".//{*}item"):
        link = (node.findtext("{*}link") or "").strip()
        title = (node.findtext("{*}title") or "").strip()
        author = node.findtext("{*}author") or node.findtext("{*}creator")
        content = (
            node.findtext("{*}description")
            or node.findtext("{*}encoded")
            or ""
        )
        entries.append(
            Entry(
                post_id=link or title,
                title=title,
                link=link,
                author=author.strip() if author else None,
                updated=(node.findtext("{*}pubDate") or "").strip() or None,
                content=content,
            )
        )
    return entries


def parse_feed(text: str) -> list[Entry]:
    """Parse an Atom or RSS 2.0 feed into entries, tolerant of both."""
    root = ET.fromstring(text)
    if root.findall("{*}entry"):
        return _parse_atom(root)
    return _parse_rss(root)


def extract_repo_urls(text: str) -> list[str]:
    """Every github.com/owner/repo in text, normalised, order preserved."""
    repos: list[str] = []
    for match in _REPO_URL_RE.finditer(text):
        owner, name = match.groups()
        name = name.rstrip(_TRAILING_PUNCT)
        if name.lower().endswith(".git"):
            name = name[:-4]
        try:
            repo = normalize_repo(f"{owner}/{name}")
        except ValueError:
            continue
        if repo not in repos:
            repos.append(repo)
    return repos


def matching_terms(config: Config, text: str) -> list[str] | None:
    """Terms matched in text, or None unless at least one agent-ish and one
    memory-ish term both appear (case-insensitive)."""
    lowered = text.lower()
    agentish = [t for t in config.terms.agentish if t.lower() in lowered]
    memoryish = [t for t in config.terms.memoryish if t.lower() in lowered]
    if not agentish or not memoryish:
        return None
    return sorted(agentish + memoryish)


def _entry_candidates(
    config: Config, store: Store, subreddit: str, entry: Entry
) -> list[Candidate]:
    store.mark_post_seen(entry.post_id, "reddit")
    text = f"{entry.title} {_strip_tags(entry.content)}"
    matched = matching_terms(config, text)
    if matched is None:
        return []
    candidates = []
    for repo in extract_repo_urls(f"{entry.title} {entry.content}"):
        candidates.append(
            Candidate(
                repo=repo,
                source="reddit",
                source_url=entry.link,
                matched_terms=matched,
                html_url=f"https://github.com/{repo}",
                subreddit=subreddit,
                author=entry.author,
                posted_at=entry.updated,
            )
        )
    return candidates


def scan(
    config: Config,
    store: Store,
    *,
    session: requests.Session | None = None,
    sleep=time.sleep,
) -> list[Candidate]:
    """Fetch each configured subreddit once and return candidates from new posts."""
    if session is None:
        session = requests.Session()
    headers = {"User-Agent": config.reddit_user_agent()}

    candidates: list[Candidate] = []
    last_request_at = 0.0
    for subreddit in config.reddit.subreddits:
        wait = config.reddit.request_interval_seconds - (
            time.monotonic() - last_request_at
        )
        if wait > 0:
            sleep(wait)
        url = f"https://www.reddit.com/r/{subreddit}/new.rss"
        response = request_with_backoff(
            lambda: session.get(url, headers=headers, timeout=30),
            should_retry=lambda response: response.status_code in BACKOFF_STATUSES,
            sleep=sleep,
            max_retries=config.reddit.max_rate_limit_retries,
            base_delay=config.reddit.request_interval_seconds,
        )
        last_request_at = time.monotonic()
        if response.status_code in BACKOFF_STATUSES:
            print(
                f"reddit: r/{subreddit} returned {response.status_code}, "
                "backing off for the rest of this run after retries",
                file=sys.stderr,
            )
            return candidates
        if response.status_code != 200:
            print(
                f"reddit: r/{subreddit} returned {response.status_code}, skipping",
                file=sys.stderr,
            )
            continue
        for entry in parse_feed(response.text):
            if store.is_post_seen(entry.post_id):
                continue
            candidates.extend(_entry_candidates(config, store, subreddit, entry))
    return candidates
