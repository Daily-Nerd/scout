from __future__ import annotations

import json
from pathlib import Path

import pytest

from scout import github_search
from scout.config import Config, load
from scout.github_search import TokenMissingError, search
from scout.store import Store

FIXTURES = Path(__file__).parent / "fixtures"

DEFAULT_CONFIG = """
[github]
queries = ["topic:agent-memory", "agent memory in:name,description"]
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
db_path = "state/scout.db"
"""


class FakeResponse:
    def __init__(self, payload):
        self._payload = payload
        self.status_code = 200

    def json(self):
        return self._payload

    def raise_for_status(self):
        return None


class FakeSession:
    """Returns canned responses per (query string, page number)."""

    def __init__(self, pages: dict[tuple[str, int], dict]):
        self.pages = pages
        self.calls: list[dict] = []

    def get(self, url, params=None, headers=None, timeout=None):
        assert url == github_search.SEARCH_URL
        q = params["q"].split(" created:>")[0]
        self.calls.append(
            {"query": q, "page": params["page"], "params": dict(params),
             "headers": dict(headers or {})}
        )
        return FakeResponse(self.pages[(q, params["page"])])


def load_config(tmp_path) -> Config:
    path = tmp_path / "scout.toml"
    path.write_text(DEFAULT_CONFIG)
    return load(path)


def payload_from_fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


def test_token_missing_raises_clear_error(tmp_path, monkeypatch):
    monkeypatch.delenv("SCOUT_GITHUB_TOKEN", raising=False)
    with Store(tmp_path / "state.db") as store:
        with pytest.raises(TokenMissingError, match="SCOUT_GITHUB_TOKEN"):
            search(load_config(tmp_path), store)


def test_maps_fields_and_excludes_forks(tmp_path):
    session = FakeSession(
        {("topic:agent-memory", 1): payload_from_fixture("github_search_page1.json"),
         ("agent memory in:name,description", 1): {"items": []}}
    )
    with Store(tmp_path / "state.db") as store:
        candidates = search(
            load_config(tmp_path), store, token="t", session=session,
            sleep=lambda s: None,
        )
    assert [c.repo for c in candidates] == [
        "daily-nerd/recallmirror",
        "someorg/contextkeeper",
    ]
    first = candidates[0]
    assert first.source == "github"
    assert first.source_url == "https://github.com/Daily-Nerd/RecallMirror"
    assert first.matched_terms == ["topic:agent-memory"]
    assert first.stars == 128
    assert first.pushed_at == "2026-09-08T10:24:00Z"
    assert first.license == "MIT"
    assert candidates[1].license is None
    assert candidates[1].description == ""


def test_appends_window_and_uses_token_header(tmp_path):
    session = FakeSession(
        {("topic:agent-memory", 1): {"items": []},
         ("agent memory in:name,description", 1): {"items": []}}
    )
    with Store(tmp_path / "state.db") as store:
        search(load_config(tmp_path), store, token="sekret",
               session=session, sleep=lambda s: None)
    assert session.calls[0]["headers"]["Authorization"] == "Bearer sekret"
    q = session.calls[0]["params"]["q"]
    assert " created:>20" in q and " pushed:>20" in q
    assert session.calls[0]["params"]["per_page"] == 100


def test_paginates_and_pauses_between_pages(tmp_path):
    page1 = {"items": [{"full_name": f"o/r{i}", "html_url": f"https://github.com/o/r{i}",
                        "fork": False, "archived": False}
                       for i in range(100)]}
    page2 = {"items": [{"full_name": "o/new", "html_url": "https://github.com/o/new",
                        "fork": False, "archived": False}]}
    session = FakeSession({("topic:agent-memory", 1): page1,
                           ("topic:agent-memory", 2): page2,
                           ("agent memory in:name,description", 1): {"items": []}})
    sleeps: list[float] = []
    with Store(tmp_path / "state.db") as store:
        candidates = search(load_config(tmp_path), store, token="t",
                            session=session, sleep=sleeps.append)
    assert len(candidates) == 101
    assert sleeps == [github_search.PAGE_PAUSE_SECONDS]
    pages = [c["page"] for c in session.calls if c["query"] == "topic:agent-memory"]
    assert pages == [1, 2]


def test_stops_paginating_when_page_is_all_seen(tmp_path):
    seen_page = {"items": [{"full_name": "o/old", "html_url": "https://github.com/o/old",
                            "fork": False, "archived": False}]}
    session = FakeSession({("topic:agent-memory", 1): seen_page,
                           ("agent memory in:name,description", 1): {"items": []}})
    with Store(tmp_path / "state.db") as store:
        store.mark_repo_seen("o/old", "github")
        candidates = search(load_config(tmp_path), store, token="t",
                            session=session, sleep=lambda s: None)
    assert candidates == []
    assert [c["page"] for c in session.calls] == [1, 1]


def test_rerun_returns_only_new_repos(tmp_path):
    page = payload_from_fixture("github_search_page1.json")
    with Store(tmp_path / "state.db") as store:
        cfg = load_config(tmp_path)
        first = search(cfg, store, token="t",
                       session=FakeSession({("topic:agent-memory", 1): page,
                                            ("agent memory in:name,description", 1): {"items": []}}),
                       sleep=lambda s: None)
        session = FakeSession({("topic:agent-memory", 1): page,
                               ("agent memory in:name,description", 1): {"items": []}})
        second = search(cfg, store, token="t", session=session, sleep=lambda s: None)
    assert len(first) == 2
    assert second == []
    # the API is still queried once per query, the store filters the rest
    assert len(session.calls) == 2
