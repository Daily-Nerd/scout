from __future__ import annotations

import base64
from datetime import UTC, datetime

import pytest
import requests

from scout.config import Config, load
from scout.refresh import RefreshOutcome, refresh
from scout.store import Store

NOW = datetime(2026, 9, 11, tzinfo=UTC)
NOW_ISO = "2026-09-11T00:00:00Z"

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
request_interval_seconds = 0
max_rate_limit_retries = 3

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

[refresh]
days = {refresh_days}
max_per_run = {refresh_max_per_run}

[state]
db_path = "{db_path}"
candidates_path = "{history_path}"
"""


def load_config(tmp_path, *, refresh_days: int = 14, refresh_max_per_run: int = 50) -> Config:
    path = tmp_path / "scout.toml"
    path.write_text(CONFIG.format(
        db_path=tmp_path / "state" / "scout.db",
        history_path=tmp_path / "data" / "candidates.jsonl",
        refresh_days=refresh_days,
        refresh_max_per_run=refresh_max_per_run,
    ))
    return load(path)


API = "https://api.github.com"


class FakeResponse:
    def __init__(self, payload=None, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"http {self.status_code}")


def _readme_payload(size: int) -> dict:
    content = base64.b64encode(b"x" * size).decode()
    return {"content": content, "encoding": "base64"}


def _tree_payload(source_files: int, has_tests: bool) -> dict:
    tree = [{"path": f"file{i}.py", "type": "blob"} for i in range(source_files)]
    if has_tests:
        tree.append({"path": "tests", "type": "tree"})
    return {"tree": tree, "truncated": False}


class FakeSession:
    """repo/tree/readme/issue-label endpoints, all keyed by repo or number."""

    def __init__(self, repo_payloads=None, tree_payloads=None, readme_payloads=None,
                 issue_labels=None, patch_status=200):
        self.repo_payloads = repo_payloads or {}
        self.tree_payloads = tree_payloads or {}
        self.readme_payloads = readme_payloads or {}
        self.issue_labels = issue_labels or {}
        self.patch_status = patch_status
        self.gets: list[str] = []
        self.patches: list[tuple[str, dict]] = []

    def get(self, url, params=None, headers=None, timeout=None):
        self.gets.append(url)
        if "/git/trees/HEAD" in url:
            repo = url.split(f"{API}/repos/")[1].split("/git/trees")[0]
            payload = self.tree_payloads.get(repo)
            if payload is None:
                return FakeResponse(status_code=404)
            return FakeResponse(payload=payload)
        if url.endswith("/readme"):
            repo = url.split(f"{API}/repos/")[1].split("/readme")[0]
            payload = self.readme_payloads.get(repo)
            if payload is None:
                return FakeResponse(status_code=404)
            if isinstance(payload, int):
                return FakeResponse(status_code=payload)
            return FakeResponse(payload=payload)
        if "/issues/" in url:
            number = int(url.rsplit("/", 1)[-1])
            labels = self.issue_labels.get(number, [])
            return FakeResponse(payload={"labels": [{"name": n} for n in labels]})
        repo = url.split(f"{API}/repos/")[1]
        outcome = self.repo_payloads.get(repo, 404)
        if isinstance(outcome, int):
            return FakeResponse(status_code=outcome)
        return FakeResponse(payload=outcome)

    def patch(self, url, headers=None, json=None, timeout=None):
        self.patches.append((url, json))
        return FakeResponse(payload={}, status_code=self.patch_status)


def _row(fetched_at: str | None = None, **overrides) -> dict:
    """A seen github row; `fetched_at` stamps its latest payload so the
    refresh selection treats it as fetched at that moment."""
    row = {
        "kind": "repo", "repo": "owner/repo",
        "discovery": "seen", "assessment": "none",
        "sources": ["github"],
        "latest": {
            "repo": "owner/repo", "source": "github",
            "source_url": "https://github.com/owner/repo",
            "matched_terms": [], "description": "", "topics": [],
            "stars": 0, "pushed_at": None, "license": None,
            "html_url": "https://github.com/owner/repo",
        },
    }
    if fetched_at is not None:
        row["latest"]["fetched_at"] = fetched_at
    row.update(overrides)
    return row


def test_selection_order_and_cap(tmp_path):
    config = load_config(tmp_path, refresh_max_per_run=2)
    store = Store(config.state.db_path, config.state.candidates_path)
    store.history.repos["owner/never-scored"] = _row(
        repo="owner/never-scored", last_seen_at="2026-09-01T00:00:00Z",
    )
    store.history.repos["owner/never-scored"]["latest"]["repo"] = "owner/never-scored"
    store.history.repos["owner/oldest-score"] = _row(
        repo="owner/oldest-score", fetched_at="2026-08-01T00:00:00Z",
        last_seen_at="2026-08-01T00:00:00Z", tier="c",
    )
    store.history.repos["owner/oldest-score"]["latest"]["repo"] = "owner/oldest-score"
    # scored today from a fresh fetch: inside the window, not due
    store.history.repos["owner/fresh-score"] = _row(
        repo="owner/fresh-score", fetched_at="2026-09-10T00:00:00Z",
        scored_at="2026-09-10T00:00:00Z",
        last_seen_at="2026-09-10T00:00:00Z", tier="c",
    )
    store.history.repos["owner/fresh-score"]["latest"]["repo"] = "owner/fresh-score"
    try:
        session = FakeSession(
            repo_payloads={
                "owner/never-scored": {"stargazers_count": 0, "pushed_at": None,
                                        "description": "", "topics": [],
                                        "license": None, "html_url": "x"},
                "owner/oldest-score": {"stargazers_count": 0, "pushed_at": None,
                                       "description": "", "topics": [],
                                       "license": None, "html_url": "x"},
            },
        )
        outcome = refresh(
            config, store, token="t", session=session,
            sleep=lambda s: None, now=NOW, apply=False,
        )
        # only the two oldest (never-scored and oldest-score) are due within
        # the cap of 2; fresh-score is excluded outright since it is inside
        # the refresh window
        assert outcome.refreshed == ["owner/never-scored", "owner/oldest-score"]
    finally:
        store.close()


def test_row_rescored_this_run_from_a_stale_fetch_is_still_refreshed(tmp_path):
    """_check_then_file rescores every pending row each run and bumps
    scored_at; that must not hide a payload last fetched in August."""
    config = load_config(tmp_path)
    store = Store(config.state.db_path, config.state.candidates_path)
    store.history.repos["owner/stale"] = _row(
        repo="owner/stale", fetched_at="2026-08-01T00:00:00Z",
        scored_at=NOW_ISO, last_seen_at="2026-08-01T00:00:00Z", tier="c",
    )
    store.history.repos["owner/stale"]["latest"]["repo"] = "owner/stale"
    try:
        session = FakeSession(
            repo_payloads={
                "owner/stale": {"stargazers_count": 0, "pushed_at": None,
                                "description": "", "topics": [],
                                "license": None, "html_url": "x"},
            },
        )
        outcome = refresh(
            config, store, token="t", session=session,
            sleep=lambda s: None, now=NOW, apply=False,
        )
        assert outcome.refreshed == ["owner/stale"]
        assert store.history.repos["owner/stale"]["latest"]["fetched_at"] == NOW_ISO
    finally:
        store.close()


def test_200_payload_updates_latest_and_rescores(tmp_path):
    config = load_config(tmp_path)
    store = Store(config.state.db_path, config.state.candidates_path)
    store.history.repos["alice/recall-hub"] = _row(
        repo="alice/recall-hub", tier="c", score=2.0,
    )
    store.history.repos["alice/recall-hub"]["latest"]["repo"] = "alice/recall-hub"
    try:
        session = FakeSession(
            repo_payloads={
                "alice/recall-hub": {
                    "stargazers_count": 500,
                    "pushed_at": NOW_ISO,
                    "description": "agent memory",
                    "topics": ["memory"],
                    "license": {"spdx_id": "MIT"},
                    "html_url": "https://github.com/alice/recall-hub",
                },
            },
            tree_payloads={
                "alice/recall-hub": _tree_payload(40, has_tests=True),
            },
            readme_payloads={
                "alice/recall-hub": _readme_payload(20000),
            },
        )
        outcome = refresh(
            config, store, token="t", session=session,
            sleep=lambda s: None, now=NOW, apply=False,
        )
        assert outcome.refreshed == ["alice/recall-hub"]
        row = store.history.repos["alice/recall-hub"]
        assert row["latest"]["stars"] == 500
        assert row["latest"]["pushed_at"] == NOW_ISO
        assert row["latest"]["description"] == "agent memory"
        assert row["latest"]["topics"] == ["memory"]
        assert row["latest"]["license"] == "MIT"
        # the fetch is stamped with the run's `now`, so the row leaves the
        # refresh queue for the next `days`
        assert row["latest"]["fetched_at"] == NOW_ISO
        # source is untouched by refresh
        assert row["latest"]["source"] == "github"
        assert row["tier"] == "a"
        assert row["score"] > 8.0
        assert store.readme_size("alice/recall-hub") == 20000
        assert store.tree_signals("alice/recall-hub") == (True, 40)
    finally:
        store.close()


def test_tier_change_patches_once_in_apply_mode_and_not_in_dry_mode(tmp_path):
    config = load_config(tmp_path)

    def build_store():
        store = Store(config.state.db_path, config.state.candidates_path)
        store.history.repos["alice/recall-hub"] = _row(
            repo="alice/recall-hub", tier="b", score=6.0, issue_number=42,
            discovery="filed",
        )
        store.history.repos["alice/recall-hub"]["latest"]["repo"] = "alice/recall-hub"
        return store

    session_kwargs = dict(
        repo_payloads={
            "alice/recall-hub": {
                "stargazers_count": 500,
                "pushed_at": NOW_ISO,
                "description": "agent memory",
                "topics": ["memory"],
                "license": {"spdx_id": "MIT"},
                "html_url": "https://github.com/alice/recall-hub",
            },
        },
        tree_payloads={"alice/recall-hub": _tree_payload(40, has_tests=True)},
        readme_payloads={"alice/recall-hub": _readme_payload(20000)},
        issue_labels={42: ["scout:candidate", "scout:tier-b"]},
    )

    apply_store = build_store()
    try:
        apply_session = FakeSession(**session_kwargs)
        outcome = refresh(
            config, apply_store, token="t", session=apply_session,
            sleep=lambda s: None, now=NOW, apply=True,
        )
        assert outcome.tier_changes == [("alice/recall-hub", "b", "a")]
        assert len(apply_session.patches) == 1
        url, payload = apply_session.patches[0]
        assert url == "https://api.github.com/repos/Daily-Nerd/scout/issues/42"
        assert payload["labels"] == ["scout:candidate", "scout:tier-a"]
    finally:
        apply_store.close()

    dry_store = build_store()
    try:
        dry_session = FakeSession(**session_kwargs)
        outcome = refresh(
            config, dry_store, token="t", session=dry_session,
            sleep=lambda s: None, now=NOW, apply=False,
        )
        assert outcome.tier_changes == [("alice/recall-hub", "b", "a")]
        assert dry_session.patches == []
    finally:
        dry_store.close()


def test_404_goes_to_failed_and_is_not_rescored(tmp_path):
    config = load_config(tmp_path)
    store = Store(config.state.db_path, config.state.candidates_path)
    store.history.repos["ghost/repo"] = _row(repo="ghost/repo", tier="c", score=2.0)
    store.history.repos["ghost/repo"]["latest"]["repo"] = "ghost/repo"
    try:
        session = FakeSession()  # no repo payloads registered -> 404
        outcome = refresh(
            config, store, token="t", session=session,
            sleep=lambda s: None, now=NOW, apply=False,
        )
        assert outcome.failed == ["ghost/repo"]
        assert outcome.refreshed == []
        row = store.history.repos["ghost/repo"]
        assert row["tier"] == "c"
        assert row["score"] == 2.0
        # no tree/readme fetch happened for a 404'd repo
        assert not any("/git/trees/" in url for url in session.gets)
        assert not any(url.endswith("/readme") for url in session.gets)
    finally:
        store.close()


def test_reddit_row_gains_stars_and_recency_after_refresh(tmp_path):
    config = load_config(tmp_path)
    store = Store(config.state.db_path, config.state.candidates_path)
    row = _row(
        repo="bob/memorymesh",
        discovery="seen",
    )
    row["latest"] = {
        "repo": "bob/memorymesh", "source": "reddit",
        "source_url": "https://www.reddit.com/r/AIMemory/comments/1/x/",
        "matched_terms": ["agent", "memory"], "description": "long term memory",
        "topics": [], "stars": None, "pushed_at": None, "license": None,
        "html_url": "https://github.com/bob/memorymesh",
        "subreddit": "AIMemory", "author": "/u/bob",
    }
    store.history.repos["bob/memorymesh"] = row
    try:
        from scout.tiering import score_candidate
        from scout.models import Candidate

        before = score_candidate(
            config,
            Candidate(
                repo="bob/memorymesh", source="reddit",
                source_url=row["latest"]["source_url"],
                description="long term memory",
            ),
            now=NOW,
        )
        assert "stars" in before.absent
        assert "recency" in before.absent

        session = FakeSession(
            repo_payloads={
                "bob/memorymesh": {
                    "stargazers_count": 250,
                    "pushed_at": NOW_ISO,
                    "description": "long term memory",
                    "topics": [],
                    "license": None,
                    "html_url": "https://github.com/bob/memorymesh",
                },
            },
            tree_payloads={"bob/memorymesh": _tree_payload(5, has_tests=False)},
            readme_payloads={},
        )
        outcome = refresh(
            config, store, token="t", session=session,
            sleep=lambda s: None, now=NOW, apply=False,
        )
        assert outcome.refreshed == ["bob/memorymesh"]
        after_row = store.history.repos["bob/memorymesh"]
        assert after_row["latest"]["stars"] == 250
        assert after_row["latest"]["pushed_at"] == NOW_ISO
        # source stays reddit: refresh never overwrites an existing source
        assert after_row["latest"]["source"] == "reddit"
        assert "stars" not in after_row["absent_components"]
        assert "recency" not in after_row["absent_components"]
        assert len(after_row["absent_components"]) < len(before.absent)
    finally:
        store.close()


def test_title_only_row_gets_latest_built_from_the_repo_payload(tmp_path):
    config = load_config(tmp_path)
    store = Store(config.state.db_path, config.state.candidates_path)
    store.history.repos["owner/title-only"] = {
        "kind": "repo", "repo": "owner/title-only",
        "discovery": "filed", "assessment": "none", "title_only": True,
        "sources": ["github"], "issue_number": 7,
    }
    try:
        session = FakeSession(
            repo_payloads={
                "owner/title-only": {
                    "stargazers_count": 10,
                    "pushed_at": NOW_ISO,
                    "description": "a memory tool",
                    "topics": [],
                    "license": None,
                    "html_url": "https://github.com/owner/title-only",
                },
            },
            tree_payloads={"owner/title-only": _tree_payload(0, has_tests=False)},
            readme_payloads={},
        )
        outcome = refresh(
            config, store, token="t", session=session,
            sleep=lambda s: None, now=NOW, apply=False,
        )
        assert outcome.refreshed == ["owner/title-only"]
        row = store.history.repos["owner/title-only"]
        assert row["latest"]["source"] == "github"
        assert row["latest"]["stars"] == 10
        assert row["latest"]["repo"] == "owner/title-only"
    finally:
        store.close()


def test_sleeps_two_seconds_between_rows(tmp_path):
    config = load_config(tmp_path)
    store = Store(config.state.db_path, config.state.candidates_path)
    for repo in ("owner/one", "owner/two"):
        store.history.repos[repo] = _row(repo=repo)
        store.history.repos[repo]["latest"]["repo"] = repo
    try:
        session = FakeSession(
            repo_payloads={
                "owner/one": {"stargazers_count": 0, "pushed_at": None,
                              "description": "", "topics": [], "license": None,
                              "html_url": "x"},
                "owner/two": {"stargazers_count": 0, "pushed_at": None,
                              "description": "", "topics": [], "license": None,
                              "html_url": "x"},
            },
        )
        sleeps: list[float] = []
        refresh(config, store, token="t", session=session, sleep=sleeps.append,
                now=NOW, apply=False)
        assert sleeps == [2, 2]
    finally:
        store.close()


def test_500_on_get_repos_lands_in_failed_and_next_row_still_refreshes(tmp_path):
    config = load_config(tmp_path)
    store = Store(config.state.db_path, config.state.candidates_path)
    store.history.repos["owner/broken"] = _row(
        repo="owner/broken", fetched_at="2026-08-01T00:00:00Z",
        last_seen_at="2026-08-01T00:00:00Z", tier="c", score=2.0,
    )
    store.history.repos["owner/broken"]["latest"]["repo"] = "owner/broken"
    store.history.repos["owner/fine"] = _row(
        repo="owner/fine", fetched_at="2026-08-02T00:00:00Z",
        last_seen_at="2026-08-02T00:00:00Z", tier="c", score=2.0,
    )
    store.history.repos["owner/fine"]["latest"]["repo"] = "owner/fine"
    try:
        session = FakeSession(
            repo_payloads={
                "owner/broken": 500,
                "owner/fine": {"stargazers_count": 0, "pushed_at": None,
                               "description": "", "topics": [], "license": None,
                               "html_url": "x"},
            },
        )
        outcome = refresh(
            config, store, token="t", session=session,
            sleep=lambda s: None, now=NOW, apply=False,
        )
        assert outcome.failed == ["owner/broken"]
        assert outcome.refreshed == ["owner/fine"]
        broken_row = store.history.repos["owner/broken"]
        assert broken_row["tier"] == "c"
        assert broken_row["score"] == 2.0
        assert broken_row["refresh_attempted_at"] == NOW_ISO
        assert broken_row["refresh_error"] == "500"
        # nothing was fetched, so the metadata stamp does not move
        assert broken_row["latest"]["fetched_at"] == "2026-08-01T00:00:00Z"
        fine_row = store.history.repos["owner/fine"]
        assert fine_row["refresh_attempted_at"] == NOW_ISO
        assert "refresh_error" not in fine_row
        # no tree/readme fetch happened for the broken repo
        assert not any("owner/broken/git" in url for url in session.gets)
        assert not any(url.endswith("owner/broken/readme") for url in session.gets)
    finally:
        store.close()


def test_500_on_readme_leaves_readme_absent_but_row_still_rescored(tmp_path):
    config = load_config(tmp_path)
    store = Store(config.state.db_path, config.state.candidates_path)
    store.history.repos["alice/recall-hub"] = _row(
        repo="alice/recall-hub", tier="c", score=2.0,
    )
    store.history.repos["alice/recall-hub"]["latest"]["repo"] = "alice/recall-hub"
    try:
        session = FakeSession(
            repo_payloads={
                "alice/recall-hub": {
                    "stargazers_count": 100, "pushed_at": NOW_ISO,
                    "description": "agent memory", "topics": [],
                    "license": None, "html_url": "https://github.com/alice/recall-hub",
                },
            },
            tree_payloads={"alice/recall-hub": _tree_payload(0, has_tests=False)},
            readme_payloads={"alice/recall-hub": 500},
        )
        outcome = refresh(
            config, store, token="t", session=session,
            sleep=lambda s: None, now=NOW, apply=False,
        )
        assert outcome.refreshed == ["alice/recall-hub"]
        assert outcome.failed == []
        row = store.history.repos["alice/recall-hub"]
        assert "readme" in row["absent_components"]
        assert store.readme_size("alice/recall-hub") is None
        # the error is not cached as an answer: the next run fetches again
        assert not store.has_readme_size("alice/recall-hub")
        assert row["refresh_attempted_at"] == NOW_ISO
        assert "refresh_error" not in row
    finally:
        store.close()


def test_failed_label_patch_does_not_stop_the_pass(tmp_path):
    config = load_config(tmp_path)
    store = Store(config.state.db_path, config.state.candidates_path)
    store.history.repos["alice/recall-hub"] = _row(
        repo="alice/recall-hub", tier="b", score=6.0, issue_number=42,
        discovery="filed",
    )
    store.history.repos["alice/recall-hub"]["latest"]["repo"] = "alice/recall-hub"
    store.history.repos["owner/fine"] = _row(
        repo="owner/fine", tier="c", score=2.0,
    )
    store.history.repos["owner/fine"]["latest"]["repo"] = "owner/fine"
    try:
        session = FakeSession(
            repo_payloads={
                "alice/recall-hub": {
                    "stargazers_count": 500, "pushed_at": NOW_ISO,
                    "description": "agent memory", "topics": ["memory"],
                    "license": {"spdx_id": "MIT"},
                    "html_url": "https://github.com/alice/recall-hub",
                },
                "owner/fine": {"stargazers_count": 0, "pushed_at": None,
                               "description": "", "topics": [], "license": None,
                               "html_url": "x"},
            },
            tree_payloads={
                "alice/recall-hub": _tree_payload(40, has_tests=True),
                "owner/fine": _tree_payload(0, has_tests=False),
            },
            readme_payloads={"alice/recall-hub": _readme_payload(20000)},
            issue_labels={42: ["scout:candidate", "scout:tier-b"]},
            patch_status=500,
        )
        outcome = refresh(
            config, store, token="t", session=session,
            sleep=lambda s: None, now=NOW, apply=True,
        )
        # the pass did not stop: the second row still got refreshed
        assert "owner/fine" in outcome.refreshed
        # the tier change is still recorded even though the label patch failed
        assert outcome.tier_changes == [("alice/recall-hub", "b", "a")]
        assert "alice/recall-hub" in outcome.failed
        row = store.history.repos["alice/recall-hub"]
        # the rescore itself went through
        assert row["tier"] == "a"
        assert row["refresh_error"] == "label patch failed"
    finally:
        store.close()


def test_unknown_old_tier_is_a_programming_error_not_a_label_failure(tmp_path):
    """Only an HTTP failure on the label patch is tolerated per row; a
    tier with no label is a bug and must surface, not land in `failed`."""
    config = load_config(tmp_path)
    store = Store(config.state.db_path, config.state.candidates_path)
    store.history.repos["alice/recall-hub"] = _row(
        repo="alice/recall-hub", tier="z", score=6.0, issue_number=42,
        discovery="filed",
    )
    store.history.repos["alice/recall-hub"]["latest"]["repo"] = "alice/recall-hub"
    try:
        session = FakeSession(
            repo_payloads={
                "alice/recall-hub": {
                    "stargazers_count": 500, "pushed_at": NOW_ISO,
                    "description": "agent memory", "topics": ["memory"],
                    "license": {"spdx_id": "MIT"},
                    "html_url": "https://github.com/alice/recall-hub",
                },
            },
            tree_payloads={"alice/recall-hub": _tree_payload(40, has_tests=True)},
            readme_payloads={"alice/recall-hub": _readme_payload(20000)},
            issue_labels={42: ["scout:candidate"]},
        )
        with pytest.raises(KeyError):
            refresh(
                config, store, token="t", session=session,
                sleep=lambda s: None, now=NOW, apply=True,
            )
    finally:
        store.close()


def test_tier_changes_recorded_even_without_an_issue_number(tmp_path):
    config = load_config(tmp_path)
    store = Store(config.state.db_path, config.state.candidates_path)
    store.history.repos["alice/recall-hub"] = _row(
        repo="alice/recall-hub", tier="c", score=2.0,
    )
    store.history.repos["alice/recall-hub"]["latest"]["repo"] = "alice/recall-hub"
    try:
        session = FakeSession(
            repo_payloads={
                "alice/recall-hub": {
                    "stargazers_count": 500, "pushed_at": NOW_ISO,
                    "description": "agent memory", "topics": ["memory"],
                    "license": {"spdx_id": "MIT"},
                    "html_url": "https://github.com/alice/recall-hub",
                },
            },
            tree_payloads={"alice/recall-hub": _tree_payload(40, has_tests=True)},
            readme_payloads={"alice/recall-hub": _readme_payload(20000)},
        )
        outcome = refresh(
            config, store, token="t", session=session,
            sleep=lambda s: None, now=NOW, apply=False,
        )
        assert outcome.tier_changes == [("alice/recall-hub", "c", "a")]
        # no issue exists, so no label patch was ever attempted
        assert session.patches == []
        assert outcome.failed == []
    finally:
        store.close()


def test_refresh_is_a_no_op_without_history(tmp_path):
    config = load_config(tmp_path)
    store = Store(config.state.db_path)
    try:
        outcome = refresh(config, store, token="t", now=NOW, apply=False)
        assert outcome == RefreshOutcome()
    finally:
        store.close()
