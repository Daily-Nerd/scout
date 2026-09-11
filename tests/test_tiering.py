from __future__ import annotations

import base64
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime

import pytest

from scout.config import Config, load
from scout.models import Candidate
from scout.store import Store
from scout.tiering import (
    TIER_A,
    TIER_B,
    TIER_C,
    TIER_LABELS,
    Component,
    _fetch_readme_size,
    _fetch_tree_signals,
    collect_readme_sizes,
    collect_tree_signals,
    component_summary,
    score_candidate,
    tree_signals_from_payload,
)

NOW = datetime(2026, 9, 11, tzinfo=UTC)

SOURCE_EXTENSIONS = [
    ".py", ".ts", ".tsx", ".js", ".go", ".rs", ".java", ".kt",
    ".rb", ".cs", ".cpp", ".c", ".swift",
]

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
tests_weight = 2.0
source_weight = 1.5
source_cap = 40
source_extensions = [".py", ".ts", ".tsx", ".js", ".go", ".rs", ".java", ".kt", ".rb", ".cs", ".cpp", ".c", ".swift"]
list_penalty_weight = 3.0
list_words = ["awesome", "list", "curated", "collection", "resources", "roundup"]
tree_max_per_run = 300

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
    assert score.components["stars"].value == 1.0
    assert score.components["recency"].value == 4.0 * (1 - 3 / 90)
    assert score.components["name"].value == 2.0
    assert score.components["readme"].value == 1.0
    assert score.components["topics"].value == 2.0
    assert score.components["density"].value == 1.5
    # tree data was not supplied, so both tree components are absent, and
    # the repo name/description trigger no list penalty
    assert score.absent == ["tests", "source"]
    assert score.components["list_penalty"].value == 0.0
    assert score.score == sum(
        c.value for c in score.components.values() if c.value is not None
    )


def test_missing_values_are_absent_not_zero(tmp_path):
    config = load_config(tmp_path)
    candidate = make_candidate(
        stars=None, pushed_at=None, topics=[], description=""
    )
    score = score_candidate(config, candidate, now=NOW)
    assert score.components["stars"] == Component(input=None, value=None)
    assert score.components["recency"].value is None
    assert score.components["readme"].value is None
    assert score.components["density"].value is None
    # topics is present (source is still github, just no topics listed)
    assert score.components["topics"].value == 0.0
    assert score.absent == ["stars", "recency", "readme", "density", "tests", "source"]
    # only the name term survives, well below tier B
    assert score.score == 2.0
    assert score.tier == TIER_C


def test_stars_zero_is_present_not_absent(tmp_path):
    config = load_config(tmp_path)
    score = score_candidate(config, make_candidate(stars=0), now=NOW)
    assert score.components["stars"].value == 0.0
    assert score.components["stars"].input == 0
    assert "stars" not in score.absent


def test_unparseable_pushed_at_leaves_recency_absent(tmp_path):
    config = load_config(tmp_path)
    candidate = make_candidate(pushed_at="not a date")
    score = score_candidate(config, candidate, now=NOW)
    assert score.components["recency"].value is None
    assert score.components["recency"].input is None
    assert "recency" in score.absent


def test_recency_clamps_outside_the_window(tmp_path):
    config = load_config(tmp_path)
    old = make_candidate(pushed_at="2026-01-01T00:00:00Z")
    assert score_candidate(config, old, now=NOW).components["recency"].value == 0.0
    future = make_candidate(pushed_at="2027-01-01T00:00:00Z")
    assert score_candidate(config, future, now=NOW).components["recency"].value == 4.0


def test_readme_component_caps_at_configured_size(tmp_path):
    config = load_config(tmp_path)
    capped = score_candidate(config, make_candidate(), readme_bytes=10**9, now=NOW)
    assert capped.components["readme"].value == 2.0
    absent = score_candidate(config, make_candidate(), readme_bytes=None, now=NOW)
    assert absent.components["readme"].value is None
    assert "readme" in absent.absent


def test_readme_zero_bytes_is_present_not_absent(tmp_path):
    config = load_config(tmp_path)
    score = score_candidate(config, make_candidate(), readme_bytes=0, now=NOW)
    assert score.components["readme"].value == 0.0
    assert score.components["readme"].input == 0
    assert "readme" not in score.absent


def test_name_component_uses_memoryish_terms_only(tmp_path):
    config = load_config(tmp_path)
    no_match = score_candidate(
        config, make_candidate(repo="alice/agent-helper"), now=NOW
    )
    assert no_match.components["name"].value == 0.0
    assert no_match.components["name"].input is None
    matched = score_candidate(
        config, make_candidate(repo="alice/recall-hub"), now=NOW
    )
    assert matched.components["name"].value == 2.0
    assert matched.components["name"].input == "recall"


def test_topics_absent_for_non_github_source(tmp_path):
    config = load_config(tmp_path)
    score = score_candidate(
        config, make_candidate(source="reddit", stars=None, pushed_at=None), now=NOW
    )
    assert score.components["stars"].value is None
    assert score.components["recency"].value is None
    assert score.components["topics"].value is None
    assert score.absent == [
        "stars", "recency", "readme", "topics", "tests", "source",
    ]
    # name and density still contribute, so the candidate still gets a tier
    assert score.score == (
        score.components["name"].value + score.components["density"].value
    )
    assert score.tier == TIER_C


def test_density_absent_when_description_has_no_words(tmp_path):
    config = load_config(tmp_path)
    score = score_candidate(
        config, make_candidate(description=""), now=NOW
    )
    assert score.components["density"].value is None
    assert score.components["density"].input is None
    assert "density" in score.absent


def test_component_summary_shows_unknown_for_absent_components(tmp_path):
    config = load_config(tmp_path)
    candidate = make_candidate(
        stars=None, pushed_at=None, topics=[], description=""
    )
    score = score_candidate(config, candidate, now=NOW)
    summary = component_summary(score)
    assert "stars=unknown" in summary
    assert "recency=unknown" in summary
    assert "readme=unknown" in summary
    assert "density=unknown" in summary
    assert "name=2.00" in summary
    assert "topics=0.00" in summary
    assert "tests=unknown" in summary
    assert "source=unknown" in summary
    assert "list_penalty=0.00" in summary


def test_scored_at_is_utc_iso_from_now(tmp_path):
    config = load_config(tmp_path)
    score = score_candidate(config, make_candidate(), readme_bytes=1000, now=NOW)
    assert score.scored_at == "2026-09-11T00:00:00Z"


def test_component_is_frozen():
    component = Component(input=1, value=2.0)
    with pytest.raises(FrozenInstanceError):
        component.value = 3.0  # type: ignore[misc]


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


def test_tests_component_present_only_when_tree_tests_known(tmp_path):
    config = load_config(tmp_path)
    present_true = score_candidate(config, make_candidate(), tree_tests=True, now=NOW)
    assert present_true.components["tests"].value == 2.0
    assert present_true.components["tests"].input is True
    assert "tests" not in present_true.absent

    present_false = score_candidate(config, make_candidate(), tree_tests=False, now=NOW)
    assert present_false.components["tests"].value == 0.0
    assert "tests" not in present_false.absent

    unknown = score_candidate(config, make_candidate(), now=NOW)
    assert unknown.components["tests"].value is None
    assert "tests" in unknown.absent


def test_source_component_scales_and_caps(tmp_path):
    config = load_config(tmp_path)
    partial = score_candidate(
        config, make_candidate(), tree_source_files=20, now=NOW
    )
    assert partial.components["source"].value == 1.5 * 20 / 40
    assert partial.components["source"].input == 20
    assert "source" not in partial.absent

    capped = score_candidate(
        config, make_candidate(), tree_source_files=1000, now=NOW
    )
    assert capped.components["source"].value == 1.5

    unknown = score_candidate(config, make_candidate(), now=NOW)
    assert unknown.components["source"].value is None
    assert "source" in unknown.absent


def test_list_penalty_applies_for_whole_word_in_repo_name(tmp_path):
    config = load_config(tmp_path)
    score = score_candidate(
        config, make_candidate(repo="alice/awesome-agent-memory"), now=NOW
    )
    assert score.components["list_penalty"].value == -3.0
    assert score.components["list_penalty"].input is True
    assert "list_penalty" not in score.absent  # always present


def test_list_penalty_applies_for_whole_word_in_description(tmp_path):
    config = load_config(tmp_path)
    score = score_candidate(
        config,
        make_candidate(description="A curated list of agent memory tools"),
        now=NOW,
    )
    assert score.components["list_penalty"].value == -3.0


def test_list_penalty_does_not_match_substrings(tmp_path):
    config = load_config(tmp_path)
    listener = score_candidate(config, make_candidate(repo="alice/listener"), now=NOW)
    assert listener.components["list_penalty"].value == 0.0

    blacklist = score_candidate(
        config,
        make_candidate(description="a blacklist of banned tools"),
        now=NOW,
    )
    assert blacklist.components["list_penalty"].value == 0.0


def test_list_penalty_is_never_absent(tmp_path):
    config = load_config(tmp_path)
    score = score_candidate(config, make_candidate(), now=NOW)
    assert "list_penalty" not in score.absent
    assert score.components["list_penalty"].value == 0.0


def test_tree_signals_detects_top_level_tests_directory():
    payload = {
        "tree": [
            {"path": "tests", "type": "tree"},
            {"path": "tests/test_app.py", "type": "blob"},
            {"path": "main.py", "type": "blob"},
        ],
        "truncated": False,
    }
    has_tests, source_files = tree_signals_from_payload(payload, SOURCE_EXTENSIONS)
    assert has_tests is True
    # both main.py and tests/test_app.py sit at or above two segments
    assert source_files == 2


def test_tree_signals_detects_nested_tests_directory():
    payload = {
        "tree": [{"path": "src/tests/util.py", "type": "blob"}],
        "truncated": False,
    }
    has_tests, _ = tree_signals_from_payload(payload, SOURCE_EXTENSIONS)
    assert has_tests is True


@pytest.mark.parametrize("basename", [
    "test_app.py", "app_test.py", "app.test.ts", "app.spec.ts", "app_test.go",
])
def test_tree_signals_detects_test_basename_patterns(basename):
    payload = {
        "tree": [{"path": f"pkg/{basename}", "type": "blob"}],
        "truncated": False,
    }
    has_tests, _ = tree_signals_from_payload(payload, SOURCE_EXTENSIONS)
    assert has_tests is True


def test_tree_signals_top_two_levels_only_count_as_source():
    payload = {
        "tree": [
            {"path": "main.py", "type": "blob"},
            {"path": "src/app.py", "type": "blob"},
            {"path": "src/a/b/c.py", "type": "blob"},
            {"path": "README.md", "type": "blob"},
        ],
        "truncated": False,
    }
    has_tests, source_files = tree_signals_from_payload(payload, SOURCE_EXTENSIONS)
    assert has_tests is False
    # README.md has no matching extension, src/a/b/c.py is three segments deep
    assert source_files == 2


def test_tree_signals_extension_match_is_case_insensitive():
    payload = {"tree": [{"path": "MAIN.PY", "type": "blob"}], "truncated": False}
    _, source_files = tree_signals_from_payload(payload, SOURCE_EXTENSIONS)
    assert source_files == 1


def test_tree_signals_counts_can_exceed_source_cap():
    payload = {
        "tree": [{"path": f"file{i}.py", "type": "blob"} for i in range(45)],
        "truncated": False,
    }
    has_tests, source_files = tree_signals_from_payload(payload, SOURCE_EXTENSIONS)
    assert has_tests is False
    assert source_files == 45  # raw count; capping happens in score_candidate


def test_tree_signals_absent_when_nothing_matches():
    payload = {"tree": [{"path": "README.md", "type": "blob"}], "truncated": False}
    has_tests, source_files = tree_signals_from_payload(payload, SOURCE_EXTENSIONS)
    assert has_tests is False
    assert source_files == 0


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


class FakeReadmeStatusSession:
    """readme -> a canned status code, no body."""

    def __init__(self, status_code: int):
        self.status_code = status_code

    def get(self, url, params=None, headers=None, timeout=None):
        return FakeResponse(status_code=self.status_code)


def test_fetch_readme_size_returns_none_on_a_non_404_error_status():
    result = _fetch_readme_size(
        FakeReadmeStatusSession(500), "alice/memorymesh", {}, sleep=lambda s: None,
    )
    assert result is None


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


class FakeTreeSession:
    """git tree endpoint -> canned payload, or a 404/409/500 status."""

    def __init__(self, outcomes: dict[str, object]):
        self.outcomes = outcomes
        self.calls: list[str] = []

    def get(self, url, headers=None, timeout=None):
        assert url.endswith("/git/trees/HEAD?recursive=1")
        repo = url.split("/repos/")[1].split("/git/trees/HEAD")[0]
        self.calls.append(repo)
        outcome = self.outcomes.get(repo, "404")
        if outcome in ("404", "409", "500"):
            return FakeResponse(status_code=int(outcome))
        return FakeResponse(payload=outcome, status_code=200)


def test_fetch_tree_signals_handles_200_404_409_and_500():
    headers = {}
    session = FakeTreeSession({
        "alice/withtests": {
            "tree": [{"path": "tests", "type": "tree"}], "truncated": False,
        },
        "alice/empty404": "404",
        "alice/empty409": "409",
        "alice/broken500": "500",
    })
    assert _fetch_tree_signals(
        session, "alice/withtests", headers,
        extensions=SOURCE_EXTENSIONS, sleep=lambda s: None,
    ) == (True, 0)
    assert _fetch_tree_signals(
        session, "alice/empty404", headers,
        extensions=SOURCE_EXTENSIONS, sleep=lambda s: None,
    ) == (False, 0)
    assert _fetch_tree_signals(
        session, "alice/empty409", headers,
        extensions=SOURCE_EXTENSIONS, sleep=lambda s: None,
    ) == (False, 0)
    assert _fetch_tree_signals(
        session, "alice/broken500", headers,
        extensions=SOURCE_EXTENSIONS, sleep=lambda s: None,
    ) is None


def test_collect_tree_signals_fetches_caches_and_skips_on_rerun(tmp_path):
    config, store = _store_with_history(tmp_path, [make_candidate()])
    session = FakeTreeSession({
        "alice/memorymesh": {
            "tree": [{"path": "main.py", "type": "blob"}], "truncated": False,
        },
    })
    try:
        result = collect_tree_signals(
            config, store, [make_candidate()], token="t", session=session,
            sleep=lambda s: None, now=NOW,
        )
        assert result == {"alice/memorymesh": (False, 1)}
        assert store.has_tree_signals("alice/memorymesh")
        assert store.tree_signals("alice/memorymesh") == (False, 1)

        again = collect_tree_signals(
            config, store, [make_candidate()], token="t", session=session,
            sleep=lambda s: None, now=NOW,
        )
        assert again == result
        assert session.calls == ["alice/memorymesh"]  # no second fetch
    finally:
        store.close()


def test_collect_tree_signals_respects_max_per_run_ordered_by_score(tmp_path):
    config = load_config(tmp_path)
    config.tiering.tree_max_per_run = 1
    low = make_candidate(
        repo="alice/low", stars=0, pushed_at=None, topics=[], description=""
    )
    high = make_candidate(
        repo="alice/recall-hub", stars=500, pushed_at="2026-09-10T00:00:00Z",
        topics=["memory"], description="agent memory",
    )
    store = Store(config.state.db_path, config.state.candidates_path)
    store.record_candidates([low, high])
    session = FakeTreeSession({
        "alice/low": {"tree": [], "truncated": False},
        "alice/recall-hub": {"tree": [], "truncated": False},
    })
    try:
        result = collect_tree_signals(
            config, store, [low, high], token="t", session=session,
            sleep=lambda s: None, now=NOW,
        )
        # the stronger preliminary score is fetched first, and the cap
        # holds the weaker candidate back for a later run
        assert session.calls == ["alice/recall-hub"]
        assert "alice/recall-hub" in result
        assert "alice/low" not in result
    finally:
        store.close()


def test_collect_tree_signals_sleeps_two_seconds_between_fetches(tmp_path):
    candidates = [make_candidate(), make_candidate(repo="bob/other")]
    config, store = _store_with_history(tmp_path, candidates)
    session = FakeTreeSession({
        "alice/memorymesh": {"tree": [], "truncated": False},
        "bob/other": {"tree": [], "truncated": False},
    })
    sleeps: list[float] = []
    try:
        collect_tree_signals(
            config, store, candidates, token="t", session=session,
            sleep=sleeps.append, now=NOW,
        )
        assert sleeps == [2, 2]
    finally:
        store.close()
