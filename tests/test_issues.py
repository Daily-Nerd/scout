from __future__ import annotations

import pytest

from scout.config import Config, load
from scout.github_search import TokenMissingError
from scout.issues import (
    REASON_HELD,
    STATUS_DRY_RUN,
    STATUS_FILED,
    STATUS_SKIPPED,
    file_candidates,
    render_body,
    render_title,
)
from scout.models import Candidate
from scout.store import Store
from scout.tiering import Component, TierScore

DEFAULT_CONFIG = """
[github]
queries = []
window_days = 14

[reddit]
subreddits = []
request_interval_seconds = 2
contact_url = "https://github.com/Daily-Nerd/scout"

[terms]
agentish = ["agent"]
memoryish = ["memory"]

[atlas]
repo = "neoneye/agent-memory-atlas"
branch = "main"
archive_org = "agent-memory-atlas-archive"

[issues]
target_repo = "Daily-Nerd/scout"
label = "scout:candidate"

[state]
db_path = "{db_path}"
"""


def load_config(tmp_path) -> Config:
    path = tmp_path / "scout.toml"
    path.write_text(DEFAULT_CONFIG.format(db_path=tmp_path / "state" / "scout.db"))
    return load(path)


def load_config_with_cap(tmp_path, cap: int) -> Config:
    path = tmp_path / "scout.toml"
    path.write_text(
        DEFAULT_CONFIG.format(db_path=tmp_path / "state" / "scout.db")
        + f"\n[filing]\nmax_per_run = {cap}\n"
    )
    return load(path)


def scored_candidate(repo: str) -> Candidate:
    return Candidate(
        repo=repo,
        source="github",
        source_url=f"https://github.com/{repo}",
        matched_terms=["topic:agent-memory"],
        description="agent memory",
        stars=5,
        pushed_at="2026-09-08T00:00:00Z",
        license="MIT",
        html_url=f"https://github.com/{repo}",
    )


def reddit_candidate() -> Candidate:
    return Candidate(
        repo="alice/memorymesh",
        source="reddit",
        source_url="https://www.reddit.com/r/AIMemory/comments/abc111/memorymesh/",
        matched_terms=["agent", "memory"],
        description="Long-term memory for agents",
        stars=42,
        pushed_at="2026-09-08T12:00:00Z",
        license="MIT",
        html_url="https://github.com/alice/memorymesh",
        subreddit="AIMemory",
        author="/u/alice_dev",
        posted_at="2026-09-08T12:00:00+00:00",
    )


def github_candidate() -> Candidate:
    return Candidate(
        repo="bob/context-store",
        source="github",
        source_url="https://github.com/bob/context-store",
        matched_terms=["topic:agent-memory"],
        description=None,
        stars=None,
        pushed_at=None,
        license=None,
        html_url="https://github.com/bob/context-store",
    )


def tier(
    repo: str,
    name: str,
    score: float,
    tier_name: str,
    absent: list[str] | None = None,
) -> TierScore:
    return TierScore(
        repo=repo, score=score, tier=tier_name,
        label=f"scout:tier-{tier_name}",
        components={"stars": Component(input=name, value=score)},
        absent=absent or [],
    )


def tiers_for(*candidates: Candidate, tier_name: str = "a", score: float = 9.0):
    return {c.repo: tier(c.repo, c.repo, score, tier_name) for c in candidates}


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
    """search/issues -> canned, label GET -> 404, everything else recorded."""

    def __init__(self, search_items=None, label_exists=False):
        self.search_items = search_items or []
        self.label_exists = label_exists
        self.posts: list[tuple[str, dict]] = []
        self.label_posts = 0

    def get(self, url, params=None, headers=None, timeout=None):
        if url.endswith("/issues"):
            items = [
                {
                    **item,
                    "labels": item.get(
                        "labels", [{"name": "scout:candidate"}]
                    ),
                }
                for item in self.search_items
            ]
            return FakeResponse(payload=items)
        if "/search/issues" in url:
            return FakeResponse(payload={"items": self.search_items})
        if "/labels/" in url:
            return FakeResponse(status_code=200 if self.label_exists else 404)
        raise AssertionError(f"unexpected GET {url}")

    def post(self, url, headers=None, json=None, timeout=None):
        if url.endswith("/labels"):
            self.label_posts += 1
            return FakeResponse(payload=json)
        if url.endswith("/issues"):
            number = 100 + len(self.posts)
            self.posts.append((url, json))
            return FakeResponse(payload={"number": number})
        raise AssertionError(f"unexpected POST {url}")


def test_render_title():
    assert render_title(reddit_candidate()) == "candidate: alice/memorymesh"


def test_render_body_reddit_source():
    body = render_body(reddit_candidate())
    assert "Repo: https://github.com/alice/memorymesh" in body
    assert "Description: Long-term memory for agents" in body
    assert "Stars: 42" in body
    assert "Last push: 2026-09-08T12:00:00Z" in body
    assert "License: MIT" in body
    assert "Source: Reddit post in r/AIMemory by /u/alice_dev: " in body
    assert "https://www.reddit.com/r/AIMemory/comments/abc111/memorymesh/" in body
    assert "Matched terms: agent, memory" in body
    assert "a person decides whether it enters the Agent Memory Atlas" in body
    assert "\u2014" not in body


def test_render_body_github_source():
    body = render_body(github_candidate())
    assert "Source: GitHub search hit, matched query: topic:agent-memory" in body
    assert "Reddit" not in body
    assert "Description: none listed" in body
    assert "Stars: unknown" in body
    assert "Last push: unknown" in body
    assert "License: none detected" in body


def test_dry_run_returns_exact_title_and_body_without_writing(tmp_path):
    with Store(tmp_path / "state.db") as store:
        results = file_candidates(
            load_config(tmp_path), store, [reddit_candidate()],
            apply=False, token=None, session=FakeSession(),
        )
        assert len(results) == 1
        result = results[0]
        assert result.status == STATUS_DRY_RUN
        assert result.title == "candidate: alice/memorymesh"
        assert result.body == render_body(reddit_candidate())
        assert not store.is_filed("alice/memorymesh")


def test_apply_files_with_tier_label_and_score_in_body(tmp_path):
    session = FakeSession()
    candidate = reddit_candidate()
    with Store(tmp_path / "state.db") as store:
        results = file_candidates(
            load_config(tmp_path), store, [candidate],
            apply=True, token="t", session=session,
            tiers=tiers_for(candidate),
        )
        assert results[0].status == STATUS_FILED
        assert results[0].issue_number == 100
        assert store.is_filed("alice/memorymesh")
        assert store.filed_issue_number("alice/memorymesh") == 100
    assert len(session.posts) == 1
    url, payload = session.posts[0]
    assert url == "https://api.github.com/repos/Daily-Nerd/scout/issues"
    assert payload["title"] == "candidate: alice/memorymesh"
    assert payload["labels"] == ["scout:candidate", "scout:tier-a"]
    assert "Tier: A (score 9.00)" in payload["body"]
    assert "Score components: stars=9.00" in payload["body"]
    assert session.label_posts == 2  # candidate label plus tier label


def test_apply_reuses_existing_label(tmp_path):
    session = FakeSession(label_exists=True)
    candidate = reddit_candidate()
    with Store(tmp_path / "state.db") as store:
        file_candidates(load_config(tmp_path), store, [candidate],
                        apply=True, token="t", session=session,
                        tiers=tiers_for(candidate))
    assert session.label_posts == 0


def test_tier_c_candidates_are_not_filed(tmp_path):
    session = FakeSession()
    candidate = reddit_candidate()
    with Store(tmp_path / "state.db") as store:
        results = file_candidates(
            load_config(tmp_path), store, [candidate],
            apply=True, token="t", session=session,
            tiers=tiers_for(candidate, tier_name="c", score=1.0),
        )
    assert results[0].status == STATUS_SKIPPED
    assert results[0].reason == "tier C"
    assert session.posts == []


def test_cap_limits_dry_run_to_top_scores_and_holds_the_rest(tmp_path):
    candidates = [scored_candidate(f"org/repo{i:02d}") for i in range(30)]
    tiers = {c.repo: tier(c.repo, c.repo, float(i), "a") for i, c in enumerate(candidates)}
    with Store(tmp_path / "state.db") as store:
        results = file_candidates(
            load_config_with_cap(tmp_path, 25), store, candidates,
            apply=False, token=None, session=FakeSession(),
            tiers=tiers,
        )
    filed = {r.repo for r in results if r.status == STATUS_DRY_RUN}
    held = {r.repo for r in results if r.status == STATUS_SKIPPED and r.reason == REASON_HELD}
    assert len(filed) == 25
    assert len(held) == 5
    # scores 0..4 are the five lowest, so they are the ones held back
    assert held == {f"org/repo{i:02d}" for i in range(5)}


def test_cap_limits_apply_run_the_same_as_dry_run(tmp_path):
    candidates = [scored_candidate(f"org/repo{i:02d}") for i in range(3)]
    tiers = {c.repo: tier(c.repo, c.repo, float(i), "a") for i, c in enumerate(candidates)}
    session = FakeSession()
    with Store(tmp_path / "state.db") as store:
        results = file_candidates(
            load_config_with_cap(tmp_path, 2), store, candidates,
            apply=True, token="t", session=session,
            tiers=tiers,
        )
    filed = [r.repo for r in results if r.status == STATUS_FILED]
    held = [r.repo for r in results if r.reason == REASON_HELD]
    assert filed == ["org/repo01", "org/repo02"]
    assert held == ["org/repo00"]
    assert len(session.posts) == 2


def test_cap_ties_are_broken_by_repo_name(tmp_path):
    candidates = [
        scored_candidate("zeta/repo"),
        scored_candidate("alpha/repo"),
        scored_candidate("mid/repo"),
    ]
    tiers = {c.repo: tier(c.repo, c.repo, 5.0, "a") for c in candidates}
    with Store(tmp_path / "state.db") as store:
        results = file_candidates(
            load_config_with_cap(tmp_path, 2), store, candidates,
            apply=False, token=None, session=FakeSession(),
            tiers=tiers,
        )
    filed = {r.repo for r in results if r.status == STATUS_DRY_RUN}
    held = {r.repo for r in results if r.reason == REASON_HELD}
    assert filed == {"alpha/repo", "mid/repo"}
    assert held == {"zeta/repo"}


def test_cap_zero_files_nothing(tmp_path):
    candidates = [scored_candidate(f"org/repo{i:02d}") for i in range(3)]
    tiers = {c.repo: tier(c.repo, c.repo, float(i), "a") for i, c in enumerate(candidates)}
    with Store(tmp_path / "state.db") as store:
        results = file_candidates(
            load_config_with_cap(tmp_path, 0), store, candidates,
            apply=False, token=None, session=FakeSession(),
            tiers=tiers,
        )
    assert all(r.status == STATUS_SKIPPED and r.reason == REASON_HELD for r in results)


def test_existing_issue_map_is_trusted_without_a_second_walk(tmp_path):
    """Passing existing skips the target-repo walk entirely."""
    class ExplodingSession(FakeSession):
        def get(self, url, params=None, headers=None, timeout=None):
            if url.endswith("/issues"):
                raise AssertionError("issue walk ran despite shared map")
            return super().get(url, params=params, headers=headers, timeout=timeout)

    candidate = reddit_candidate()
    with Store(tmp_path / "state.db") as store:
        results = file_candidates(
            load_config(tmp_path), store, [candidate],
            apply=True, token="t", session=ExplodingSession(),
            tiers=tiers_for(candidate),
            existing={"alice/memorymesh": 55},
        )
    assert results[0].status == STATUS_SKIPPED
    assert results[0].issue_number == 55


def test_render_body_with_tier_shows_components():
    candidate = reddit_candidate()
    body = render_body(candidate, tier("alice/memorymesh", "x", 7.25, "b"))
    assert "Tier: B (score 7.25)" in body
    assert "Score components: stars=7.25" in body
    assert "\u2014" not in body


def test_render_body_shows_partial_marker_when_components_are_absent():
    candidate = reddit_candidate()
    partial_tier = tier(
        "alice/memorymesh", "x", 3.0, "c", absent=["stars", "topics"]
    )
    body = render_body(candidate, partial_tier)
    assert "Tier: C (score 3.00) (partial: stars, topics)" in body


def test_render_body_omits_partial_marker_when_nothing_is_absent():
    candidate = reddit_candidate()
    body = render_body(candidate, tier("alice/memorymesh", "x", 9.0, "a"))
    assert "Tier: A (score 9.00)" in body
    assert "partial" not in body


def test_apply_files_and_marks_store(tmp_path):
    session = FakeSession()
    with Store(tmp_path / "state.db") as store:
        results = file_candidates(
            load_config(tmp_path), store, [reddit_candidate()],
            apply=True, token="t", session=session,
        )
        assert results[0].status == STATUS_FILED
        assert results[0].issue_number == 100
        assert store.is_filed("alice/memorymesh")
        assert store.filed_issue_number("alice/memorymesh") == 100
    assert len(session.posts) == 1
    url, payload = session.posts[0]
    assert url == "https://api.github.com/repos/Daily-Nerd/scout/issues"
    assert payload["title"] == "candidate: alice/memorymesh"
    assert payload["labels"] == ["scout:candidate"]
    assert payload["body"] == render_body(reddit_candidate())
    assert session.label_posts == 1  # label created once


def test_apply_reuses_existing_label(tmp_path):
    session = FakeSession(label_exists=True)
    with Store(tmp_path / "state.db") as store:
        file_candidates(load_config(tmp_path), store, [reddit_candidate()],
                        apply=True, token="t", session=session)
    assert session.label_posts == 0


def test_skips_repo_already_in_store(tmp_path):
    with Store(tmp_path / "state.db") as store:
        store.mark_filed("alice/memorymesh", 7)
        results = file_candidates(
            load_config(tmp_path), store, [reddit_candidate()],
            apply=True, token="t", session=FakeSession(),
        )
    assert results[0].status == STATUS_SKIPPED
    assert results[0].reason == "issue already filed"
    assert results[0].issue_number == 7


def test_skips_repo_with_existing_issue_and_records_number(tmp_path):
    session = FakeSession(search_items=[
        {"title": "candidate: alice/memorymesh", "number": 55},
    ])
    with Store(tmp_path / "state.db") as store:
        results = file_candidates(
            load_config(tmp_path), store, [reddit_candidate()],
            apply=True, token="t", session=session,
        )
        assert store.filed_issue_number("alice/memorymesh") == 55
    assert results[0].status == STATUS_SKIPPED
    assert results[0].issue_number == 55
    assert session.posts == []


def test_apply_without_token_fails_clearly(tmp_path):
    with Store(tmp_path / "state.db") as store:
        with pytest.raises(TokenMissingError, match="SCOUT_GITHUB_TOKEN"):
            file_candidates(load_config(tmp_path), store, [reddit_candidate()],
                            apply=True, token=None, session=FakeSession())
