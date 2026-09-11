"""Deterministic candidate tiering.

Every candidate that survives the atlas check gets a numeric score built
from nine components: stars, recency of the last push, a memory-ish term
in the repo name, README size, topic hits, term density in the
description, whether the git tree has a tests directory, a count of
top-level source files, and a penalty for awesome-list-style repos.
Weights and thresholds come from the [tiering] config section.

A component whose underlying input is missing is absent, not zero: it
is left out of the total and out of the tier decision rather than
dragging the score down. `TierScore.absent` lists which components had
no input, in a fixed order, so a curator (and the issue body and run
report) can tell "no signal" from "signal, and it was zero". Scoring
never raises. `list_penalty` is the one component that is always
present, since it only needs the repo name and description, which
every candidate carries.

README size and git tree signals each need a per-repo API call, so they
are fetched only for candidates that survived the atlas check and are
cached in the history row (the readme_bytes, tree_tests and
tree_source_files fields of the latest payload) so re-runs do not
refetch them.
"""

from __future__ import annotations

import base64
import re
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime

import requests

from .config import Config
from .models import Candidate
from .rate_limit import github_rate_limited, request_with_backoff
from .store import Store

API = "https://api.github.com"


def _headers(token: str | None) -> dict[str, str]:
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "scout"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers

TIER_A = "a"
TIER_B = "b"
TIER_C = "c"
TIER_LABELS = {TIER_A: "scout:tier-a", TIER_B: "scout:tier-b", TIER_C: "scout:tier-c"}

REASON_TIER_C = "tier C"

_COMPONENT_NAMES = (
    "stars", "recency", "name", "readme", "topics", "density",
    "tests", "source", "list_penalty",
)

_WORD_RE = re.compile(r"[a-z0-9]+")

_TEST_BASENAME_RE = re.compile(
    r"^(test_.+\.py|.+_test\.py|.+\.test\.ts|.+\.spec\.ts|.+_test\.go)$"
)


@dataclass(frozen=True)
class Component:
    """One score component: the raw input behind it and the value it produced.

    Both are None when the underlying input was missing, i.e. the
    component is absent (unknown), not scored as zero. `input` must stay
    JSON-serializable (str, int, float, bool, list, or None) since a
    TierScore is eventually written out as part of a JSON row.
    """

    input: object | None
    value: float | None


_ABSENT = Component(input=None, value=None)


@dataclass
class TierScore:
    """One candidate's score, its components, and the tier they imply."""

    repo: str
    score: float
    tier: str  # a, b or c
    label: str  # scout:tier-a and friends
    components: dict[str, Component] = field(default_factory=dict)
    absent: list[str] = field(default_factory=list)
    scored_at: str = ""


def _days_since(pushed_at: str | None, now: datetime) -> float | None:
    if not pushed_at:
        return None
    try:
        parsed = datetime.fromisoformat(pushed_at.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return (now - parsed).total_seconds() / 86400


def _term_density(description: str, terms: list[str]) -> float:
    words = description.lower().split()
    if not words:
        return 0.0
    lowered = [term.lower() for term in terms]
    hits = sum(1 for word in words if any(term in word for term in lowered))
    return hits / len(words)


def _has_list_word(text: str, words: list[str]) -> bool:
    """True if any word from `words` appears as a whole word in `text`,
    case-insensitive. `-` and `_` count as separators alongside whitespace
    and punctuation, so "awesome-agent-memory" matches "awesome" but
    "blacklist" does not match "list"."""
    if not text:
        return False
    tokens = set(_WORD_RE.findall(text.lower()))
    return any(word.lower() in tokens for word in words)


def score_candidate(
    config: Config,
    candidate: Candidate,
    *,
    readme_bytes: int | None = None,
    tree_tests: bool | None = None,
    tree_source_files: int | None = None,
    now: datetime | None = None,
) -> TierScore:
    """Score a candidate on the nine configured components. Pure arithmetic.

    Each component is either present, with an `input` and a `value`, or
    absent, with both `None`, when the data it needs was never known. The
    total only sums present values, and the tier is decided from that
    total, so absent components neither help nor hurt the score.
    `list_penalty` is always present.
    """
    tiering = config.tiering
    now = now or datetime.now(UTC)
    terms = config.terms

    components: dict[str, Component] = {}

    if candidate.stars is None:
        components["stars"] = _ABSENT
    else:
        capped_stars = min(candidate.stars, tiering.stars_cap)
        stars_value = tiering.stars_weight * capped_stars / tiering.stars_cap
        components["stars"] = Component(input=candidate.stars, value=stars_value)

    days = _days_since(candidate.pushed_at, now)
    if days is None:
        components["recency"] = _ABSENT
    else:
        recency_value = tiering.recency_weight * min(
            1.0, max(0.0, 1.0 - days / tiering.recency_window_days)
        )
        components["recency"] = Component(input=round(days, 1), value=recency_value)

    repo_name = candidate.repo.partition("/")[2]
    matched_term = next(
        (term for term in terms.memoryish if term.lower() in repo_name.lower()),
        None,
    )
    name_value = tiering.name_weight if matched_term is not None else 0.0
    components["name"] = Component(input=matched_term, value=name_value)

    if readme_bytes is None:
        components["readme"] = _ABSENT
    else:
        readme_value = (
            tiering.readme_weight
            * min(readme_bytes, tiering.readme_cap_bytes)
            / tiering.readme_cap_bytes
        )
        components["readme"] = Component(input=readme_bytes, value=readme_value)

    if candidate.source != "github":
        components["topics"] = _ABSENT
    else:
        hits = terms.match(" ".join(candidate.topics))
        topics_value = tiering.topic_weight * len(hits)
        components["topics"] = Component(input=hits, value=topics_value)

    words = candidate.description.split()
    if not words:
        components["density"] = _ABSENT
    else:
        fraction = _term_density(
            candidate.description, [*terms.agentish, *terms.memoryish]
        )
        density_value = tiering.density_weight * fraction
        components["density"] = Component(input=round(fraction, 3), value=density_value)

    if tree_tests is None:
        components["tests"] = _ABSENT
    else:
        tests_value = tiering.tests_weight if tree_tests else 0.0
        components["tests"] = Component(input=tree_tests, value=tests_value)

    if tree_source_files is None:
        components["source"] = _ABSENT
    else:
        source_value = (
            tiering.source_weight
            * min(tree_source_files, tiering.source_cap)
            / tiering.source_cap
        )
        components["source"] = Component(input=tree_source_files, value=source_value)

    is_list_like = _has_list_word(repo_name, tiering.list_words) or _has_list_word(
        candidate.description, tiering.list_words
    )
    list_penalty_value = -tiering.list_penalty_weight if is_list_like else 0.0
    components["list_penalty"] = Component(input=is_list_like, value=list_penalty_value)

    total = sum(
        component.value
        for component in components.values()
        if component.value is not None
    )
    if total >= tiering.tier_a_min:
        tier = TIER_A
    elif total >= tiering.tier_b_min:
        tier = TIER_B
    else:
        tier = TIER_C

    absent = [
        component_name
        for component_name in _COMPONENT_NAMES
        if components[component_name].value is None
    ]

    return TierScore(
        repo=candidate.repo,
        score=total,
        tier=tier,
        label=TIER_LABELS[tier],
        components=components,
        absent=absent,
        scored_at=now.strftime("%Y-%m-%dT%H:%M:%SZ"),
    )


def _readme_size_from_payload(payload: dict) -> int | None:
    content = payload.get("content")
    if not content:
        return None
    if payload.get("encoding") == "base64":
        return len(base64.b64decode("".join(str(content).split())))
    return len(str(content))


def _fetch_readme_size(
    session: requests.Session, repo: str, headers: dict[str, str], *, sleep=time.sleep
) -> int | None:
    """README byte size for one repo, or None when there is no README."""
    response = request_with_backoff(
        lambda: session.get(
            f"{API}/repos/{repo}/readme", headers=headers, timeout=30
        ),
        should_retry=github_rate_limited,
        sleep=sleep,
    )
    if response.status_code == 404:
        return None
    response.raise_for_status()
    return _readme_size_from_payload(response.json())


def collect_readme_sizes(
    config: Config,
    store: Store,
    candidates: list[Candidate],
    *,
    token: str | None,
    session: requests.Session | None = None,
    sleep=time.sleep,
) -> dict[str, int | None]:
    """README sizes for the given candidates, reusing the history cache.

    Repos whose history row already carries a readme_bytes value are not
    fetched again; repos without one are fetched once and the size is
    written back into the history row so later runs skip them too.
    """
    if session is None:
        session = requests.Session()
    headers = _headers(token)

    sizes: dict[str, int | None] = {}
    pending: list[Candidate] = []
    for candidate in candidates:
        if store.has_readme_size(candidate.repo):
            sizes[candidate.repo] = store.readme_size(candidate.repo)
        else:
            pending.append(candidate)
    for candidate in pending:
        size = _fetch_readme_size(session, candidate.repo, headers, sleep=sleep)
        sizes[candidate.repo] = size
        store.save_readme_size(candidate.repo, size)
    return sizes


def tree_signals_from_payload(
    payload: dict, source_extensions: list[str]
) -> tuple[bool, int]:
    """(has_tests, source_file_count) from a git tree API payload.

    `has_tests` is true when any entry's path (file or directory) has a
    first or second segment of "tests" or "test", or a blob's basename
    matches a common test-file naming pattern. `source_file_count` counts
    blobs whose path has at most two segments (the top two directory
    levels) and whose extension is in `source_extensions`; it is a raw
    count, not capped here.
    """
    has_tests = False
    source_file_count = 0
    for item in payload.get("tree") or []:
        path = item.get("path") or ""
        if not path:
            continue
        segments = path.split("/")
        if segments[0] in ("tests", "test"):
            has_tests = True
        elif len(segments) > 1 and segments[1] in ("tests", "test"):
            has_tests = True

        if item.get("type") != "blob":
            continue
        basename = segments[-1]
        if _TEST_BASENAME_RE.match(basename):
            has_tests = True
        if len(segments) <= 2 and "." in basename:
            extension = "." + basename.rpartition(".")[2]
            if extension in source_extensions:
                source_file_count += 1
    return has_tests, source_file_count


def _fetch_tree_signals(
    session: requests.Session,
    repo: str,
    headers: dict[str, str],
    *,
    extensions: list[str],
    sleep=time.sleep,
) -> tuple[bool, int] | None:
    """Tree signals for one repo, or None when the fetch itself failed.

    A 404 or 409 (empty repo, no default branch yet) is a known, scorable
    state: (False, 0). Any other error leaves the signals unknown so a
    later run can retry rather than caching a wrong answer.
    """
    response = request_with_backoff(
        lambda: session.get(
            f"{API}/repos/{repo}/git/trees/HEAD?recursive=1",
            headers=headers, timeout=30,
        ),
        should_retry=github_rate_limited,
        sleep=sleep,
    )
    if response.status_code in (404, 409):
        return False, 0
    try:
        response.raise_for_status()
        payload = response.json()
    except Exception:
        return None
    return tree_signals_from_payload(payload, extensions)


def collect_tree_signals(
    config: Config,
    store: Store,
    candidates: list[Candidate],
    *,
    token: str | None,
    session: requests.Session | None = None,
    sleep=time.sleep,
    now: datetime | None = None,
) -> dict[str, tuple[bool, int] | None]:
    """Tree signals for the given candidates, reusing the history cache.

    Repos already cached are never refetched. Among the rest, at most
    `tree_max_per_run` are fetched this run, in descending order of a
    preliminary score (today's readme_bytes cache, no tree data), so the
    strongest candidates get tree data first; a repo left out this run is
    simply absent from the result and stays a candidate for the next one.
    """
    if session is None:
        session = requests.Session()
    headers = _headers(token)
    tiering = config.tiering

    results: dict[str, tuple[bool, int] | None] = {}
    pending: list[Candidate] = []
    for candidate in candidates:
        if store.has_tree_signals(candidate.repo):
            results[candidate.repo] = store.tree_signals(candidate.repo)
        else:
            pending.append(candidate)

    def preliminary_score(candidate: Candidate) -> float:
        readme_bytes = (
            store.readme_size(candidate.repo)
            if store.has_readme_size(candidate.repo) else None
        )
        return score_candidate(
            config, candidate, readme_bytes=readme_bytes, now=now
        ).score

    pending.sort(key=preliminary_score, reverse=True)

    for candidate in pending[: tiering.tree_max_per_run]:
        signals = _fetch_tree_signals(
            session, candidate.repo, headers,
            extensions=tiering.source_extensions, sleep=sleep,
        )
        results[candidate.repo] = signals
        if signals is not None:
            has_tests, source_files = signals
            fetched_at = (now or datetime.now(UTC)).strftime("%Y-%m-%dT%H:%M:%SZ")
            store.save_tree_signals(candidate.repo, has_tests, source_files, fetched_at)
        sleep(2)
    return results


def component_summary(score: TierScore) -> str:
    """Human-readable component breakdown for issue bodies and reports.

    Present components render as `name=value` to two decimals; absent
    ones render as `name=unknown` so a curator never mistakes "no
    signal" for a real zero.
    """
    parts = []
    for name in _COMPONENT_NAMES:
        component = score.components.get(name, _ABSENT)
        if component.value is None:
            parts.append(f"{name}=unknown")
        else:
            parts.append(f"{name}={component.value:.2f}")
    return ", ".join(parts)
