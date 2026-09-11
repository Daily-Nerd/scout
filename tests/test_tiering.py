from __future__ import annotations

import base64
from datetime import UTC, datetime

from scout.config import Config, load
from scout.models import Candidate
from scout.store import Store
from scout.tiering import (
    TIER_A,
    TIER_B,
    TIER_C,
    TIER_LABELS,
    collect_readme_sizes,
    score_candidate,
)

NOW = datetime(2026, 9, 11, tzinfo=UTC)

CONFIG = """
[github]
queries = []
window_days = 14

[reddit]
subreddits = []
request_interval_seconds = 2
contact_url = "https://github.com/Daily-Nerd/scout"

[terms]
agentish = ["agent", "assistant"]
memoryish = ["memory", "recall"]

[atlas]
repo = "neoneye/agent-memory-atlas"
branch = "main"
archive_org = "agent-memory-atlas-archive"

[issues]
target_repo = "Daily-Nerd/scout"
label = "scout:candidate"

[tiering]
stars_weight = 2.0
stars_cap = 500
recency_weight = 4.0
recency_window_days = 90
name_weight = 2.0
readme_weight = 2.0
readme_cap_bytes = 20000
topic_weight = 1.0
density_weight = 3.0
tier_a_min = 8.0
tier_b_min = 5.0

[state]
db_path = "{db_path}"
candidates_path = "{history_path}"
"""


def load_config(tmp_path) -> Config:
    path = tmp_path / "scout.toml"
    path.write_text(CONFIG.format(
        db_path=tmp_path / "state" / "scout.db",
        history_path=tmp_path / "data" / "candidates.jsonl",
    ))
    return load(path)


def make_candidate(**overrides) -> Candidate:
    fields = dict(
        repo="alice/memorymesh",
        source="github",
        source_url="https://github.com/alice/memorymesh",
        matched_terms=["topic:agent-memory"],
        description="memory store for agents",
        topics=["agent-memory"],
        stars=250,
        pushed_at="2026-09-08T00:00:00Z",
        html_url="https://github.com/alice/memorymesh",
    )
    fields.update(overrides)
    return Candidate(**fields)


def test_full_score_matches_hand_computation(tmp_path):
    config = load_config(tmp_path)
    candidate = make_candidate()
    score = score_candidate(config, candidate, readme_bytes=10000, now=NOW)
    # stars: 2 * 250/500 = 1.0
    # recency: 3 of 90 days old -> 4 * (1 - 3/90) = 3.8666...
    # name: "memory" in memorymesh -> 2.0
    # readme: 2 * 10000/20000 = 1.0
    # topics: "agent-memory" matches both families -> 2.0
    # density: "memory" and "agents" match, 2 of 4 words -> 3 * 0.5 = 1.5
    assert score.components["stars"] == 1.0
    assert score.components["recency"] == 4.0 * (1 - 3 / 90)
    assert score.components["name"] == 2.0
    assert score.components["readme"] == 1.0
    assert score.components["topics"] == 2.0
    assert score.components["density"] == 1.5
    assert score.score == sum(score.components.values())


def test_missing_values_score_zero_components(tmp_path):
    config = load_config(tmp_path)
    candidate = make_candidate(
        stars=None, pushed_at=None, topics=[], description=""
    )
    score = score_candidate(config, candidate, now=NOW)
    assert score.components["stars"] == 0.0
    assert score.components["recency"] == 0.0
    assert score.components["readme"] == 0.0
    assert score.components["topics"] == 0.0
    assert score.components["density"] == 0.0
    # only the name term survives, well below tier B
    assert score.tier == TIER_C


def test_unparseable_pushed_at_scores_zero_recency(tmp_path):
    config = load_config(tmp_path)
    candidate = make_candidate(pushed_at="not a date")
    score = score_candidate(config, candidate, now=NOW)
    assert score.components["recency"] == 0.0


def test_recency_clamps_outside_the_window(tmp_path):
    config = load_config(tmp_path)
    old = make_candidate(pushed_at="2026-01-01T00:00:00Z")
    assert score_candidate(config, old, now=NOW).components["recency"] == 0.0
    future = make_candidate(pushed_at="2027-01-01T00:00:00Z")
    assert score_candidate(config, future, now=NOW).components["recency"] == 4.0


def test_readme_component_caps_at_configured_size(tmp_path):
    config = load_config(tmp_path)
    capped = score_candidate(config, make_candidate(), readme_bytes=10**9, now=NOW)
    assert capped.components["readme"] == 2.0
    none = score_candidate(config, make_candidate(), readme_bytes=None, now=NOW)
    assert none.components["readme"] == 0.0


def test_name_component_uses_memoryish_terms_only(tmp_path):
    config = load_config(tmp_path)
    assert score_candidate(
        config, make_candidate(repo="alice/agent-helper"), now=NOW
    ).components["name"] == 0.0
    assert score_candidate(
        config, make_candidate(repo="alice/recall-hub"), now=NOW
    ).components["name"] == 2.0


def test_tier_threshold_boundaries(tmp_path):
    config = load_config(tmp_path)
    # zero everything, then lean on single components to land on boundaries
    base = make_candidate(
        stars=0, pushed_at=None, topics=[], description="plain words here"
    )
    quiet = score_candidate(config, base, now=NOW)
    assert quiet.score == 2.0 and quiet.tier == TIER_C  # name term only

    # 2.0 name + 2.0 full readme + 1.0 topic hit = 5.0, exactly tier B
    at_b = make_candidate(
        repo="alice/memorymesh", stars=0, pushed_at=None,
        topics=["memory"], description="plain words here",
    )
    exact_b = score_candidate(config, at_b, readme_bytes=20000, now=NOW)
    assert exact_b.score == 5.0
    assert exact_b.tier == TIER_B

    # same but one fewer topic hit: 4.0, one point below tier B
    below_b = make_candidate(
        repo="alice/memorymesh", stars=0, pushed_at=None, topics=[], description="x"
    )
    almost_b = score_candidate(config, below_b, readme_bytes=20000, now=NOW)
    assert almost_b.score == 4.0
    assert almost_b.tier == TIER_C

    # 2.0 name + 2.0 full readme + 1.0 topic + 3.0 full density = 8.0,
    # exactly tier A
    at_a = make_candidate(
        repo="alice/recall-hub", stars=0, pushed_at=None,
        topics=["memory"], description="agent memory",
    )
    exact_a = score_candidate(config, at_a, readme_bytes=20000, now=NOW)
    assert exact_a.score == 8.0
    assert exact_a.tier == TIER_A
    assert exact_a.label == TIER_LABELS[TIER_A]

    # density 2/3 words instead of 2/2: 7.0, one point below tier A
    below_a = make_candidate(
        repo="alice/recall-hub", stars=0, pushed_at=None,
        topics=["memory"], description="agent robot memory",
    )
    almost_a = score_candidate(config, below_a, readme_bytes=20000, now=NOW)
    assert almost_a.score == 7.0
    assert almost_a.tier == TIER_B


class FakeResponse:
    def __init__(self, payload=None, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"http {self.status_code}")


class FakeSession:
    """readme -> canned base64 payload or 404, everything else fails."""

    def __init__(self, sizes: dict[str, int | None]):
        self.sizes = sizes
        self.readme_calls: list[str] = []

    def get(self, url, params=None, headers=None, timeout=None):
        assert "/readme" in url
        repo = url.split("/repos/")[1].split("/readme")[0]
        self.readme_calls.append(repo)
        size = self.sizes.get(repo)
        if size is None:
            return FakeResponse(status_code=404)
        content = base64.b64encode(b"x" * size).decode()
        return FakeResponse(payload={"content": content, "encoding": "base64"})


def _store_with_history(tmp_path, candidates):
    config = load_config(tmp_path)
    store = Store(config.state.db_path, config.state.candidates_path)
    store.record_candidates(candidates)
    return config, store


def test_readme_sizes_fetch_decode_and_cache(tmp_path):
    config, store = _store_with_history(
        tmp_path, [make_candidate(), make_candidate(repo="bob/noreadme")]
    )
    session = FakeSession({"alice/memorymesh": 5000, "bob/noreadme": None})
    try:
        sizes = collect_readme_sizes(
            config, store, [make_candidate(), make_candidate(repo="bob/noreadme")],
            token="t", session=session,
        )
        assert sizes == {"alice/memorymesh": 5000, "bob/noreadme": None}
        assert session.readme_calls == ["alice/memorymesh", "bob/noreadme"]
        assert store.readme_size("alice/memorymesh") == 5000
        assert store.has_readme_size("bob/noreadme")

        # a second run over the same repos makes no readme calls at all,
        # including the repo whose README was missing
        again = collect_readme_sizes(
            config, store, [make_candidate(), make_candidate(repo="bob/noreadme")],
            token="t", session=session,
        )
        assert again == sizes
        assert session.readme_calls == ["alice/memorymesh", "bob/noreadme"]
    finally:
        store.close()


def test_readme_cache_survives_a_fresh_store(tmp_path):
    candidates = [make_candidate()]
    config, store = _store_with_history(tmp_path, candidates)
    session = FakeSession({"alice/memorymesh": 1234})
    try:
        collect_readme_sizes(config, store, candidates, token="t", session=session)
    finally:
        store.close()
    fresh = Store(config.state.db_path, config.state.candidates_path)
    try:
        assert fresh.readme_size("alice/memorymesh") == 1234
        collect_readme_sizes(
            config, fresh, candidates, token="t",
            session=FakeSession({"alice/memorymesh": 9999}),
        )
        assert fresh.readme_size("alice/memorymesh") == 1234
    finally:
        fresh.close()
