"""Deterministic candidate tiering.

Every candidate that survives the atlas check gets a numeric score built
from six arithmetic components: stars, recency of the last push, a
memory-ish term in the repo name, README size, topic hits, and term
density in the description. Weights and thresholds come from the
[tiering] config section. Missing data scores zero for that component;
it never raises.

README size needs a per-repo API call, so it is fetched only for
candidates that survived the atlas check and is cached in the history
row (the readme_bytes field of the latest payload) so re-runs do not
refetch it.
"""

from __future__ import annotations

import base64
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

_COMPONENT_NAMES = ("stars", "recency", "name", "readme", "topics", "density")


@dataclass
class TierScore:
    """One candidate's score, its components, and the tier they imply."""

    repo: str
    score: float
    tier: str  # a, b or c
    label: str  # scout:tier-a and friends
    components: dict[str, float] = field(default_factory=dict)


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


def score_candidate(
    config: Config,
    candidate: Candidate,
    *,
    readme_bytes: int | None = None,
    now: datetime | None = None,
) -> TierScore:
    """Score a candidate on the six configured components. Pure arithmetic."""
    tiering = config.tiering
    now = now or datetime.now(UTC)
    terms = config.terms

    capped_stars = min(candidate.stars or 0, tiering.stars_cap)
    stars = tiering.stars_weight * capped_stars / tiering.stars_cap

    days = _days_since(candidate.pushed_at, now)
    recency = 0.0
    if days is not None:
        recency = tiering.recency_weight * min(
            1.0, max(0.0, 1.0 - days / tiering.recency_window_days)
        )

    name = candidate.repo.partition("/")[2]
    name_score = (
        tiering.name_weight
        if any(term.lower() in name.lower() for term in terms.memoryish)
        else 0.0
    )

    readme = 0.0
    if readme_bytes:
        readme = tiering.readme_weight * min(
            readme_bytes, tiering.readme_cap_bytes
        ) / tiering.readme_cap_bytes

    topic_hits = len(terms.match(" ".join(candidate.topics)))
    topics = tiering.topic_weight * topic_hits

    density = tiering.density_weight * _term_density(
        candidate.description, [*terms.agentish, *terms.memoryish]
    )

    components = {
        "stars": stars,
        "recency": recency,
        "name": name_score,
        "readme": readme,
        "topics": topics,
        "density": density,
    }
    total = sum(components.values())
    if total >= tiering.tier_a_min:
        tier = TIER_A
    elif total >= tiering.tier_b_min:
        tier = TIER_B
    else:
        tier = TIER_C
    return TierScore(
        repo=candidate.repo,
        score=total,
        tier=tier,
        label=TIER_LABELS[tier],
        components=components,
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


def component_summary(score: TierScore) -> str:
    """Human-readable component breakdown for issue bodies and reports."""
    parts = ", ".join(
        f"{name} {score.components.get(name, 0.0):.2f}"
        for name in _COMPONENT_NAMES
    )
    return parts
