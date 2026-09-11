from __future__ import annotations

import pytest

from scout.config import Config, load
from scout.github_search import TokenMissingError
from scout.history import History
from scout.retract import (
    EXPLAINER_BODY,
    EXPLAINER_TITLE,
    RETRACTED_LABEL,
    retract,
)

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
request_interval_seconds = 2

[state]
db_path = "{db_path}"
"""


def load_config(tmp_path) -> Config:
    path = tmp_path / "scout.toml"
    path.write_text(DEFAULT_CONFIG.format(db_path=tmp_path / "state" / "scout.db"))
    return load(path)


def open_candidate(number: int, labels=None):
    return {
        "number": number,
        "title": f"candidate: owner/repo{number}",
        "labels": [{"name": name} for name in (labels or ["scout:candidate"])],
    }


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
    """issues list -> canned; PATCH/POST/PUT recorded."""

    def __init__(self, issues=None, label_exists=False, pin_status=204):
        self.issues = issues or []
        self.label_exists = label_exists
        self.pin_status = pin_status
        self.gets: list[dict] = []
        self.patches: list[tuple[str, dict]] = []
        self.posts: list[tuple[str, dict]] = []
        self.puts: list[str] = []
        self.label_posts = 0

    def get(self, url, params=None, headers=None, timeout=None):
        if url.endswith("/issues"):
            self.gets.append(dict(params or {}))
            return FakeResponse(payload=list(self.issues))
        if "/labels/" in url:
            return FakeResponse(status_code=200 if self.label_exists else 404)
        raise AssertionError(f"unexpected GET {url}")

    def patch(self, url, headers=None, json=None, timeout=None):
        self.patches.append((url, json))
        return FakeResponse(payload={})

    def post(self, url, headers=None, json=None, timeout=None):
        if url.endswith("/labels"):
            self.label_posts += 1
            return FakeResponse(payload=json)
        if url.endswith("/issues"):
            self.posts.append((url, json))
            return FakeResponse(payload={"number": 9000 + len(self.posts)})
        raise AssertionError(f"unexpected POST {url}")

    def put(self, url, headers=None, timeout=None):
        self.puts.append(url)
        return FakeResponse(status_code=self.pin_status)


def test_dry_run_lists_without_writing(tmp_path):
    session = FakeSession(issues=[open_candidate(n) for n in (3, 4, 5)])
    result = retract(load_config(tmp_path), apply=False, token=None, session=session)
    assert result.found == 3
    assert [issue[0] for issue in result.issues] == [3, 4, 5]
    assert session.patches == [] and session.posts == [] and session.puts == []


def test_apply_closes_each_with_retracted_label(tmp_path):
    session = FakeSession(
        issues=[open_candidate(7), open_candidate(8, ["scout:candidate", "bug"])],
        label_exists=True,
    )
    sleeps: list[float] = []
    result = retract(
        load_config(tmp_path), apply=True, token="t", session=session,
        sleep=sleeps.append,
    )
    assert result.closed == 2
    assert len(session.patches) == 2
    url, payload = session.patches[0]
    assert url == "https://api.github.com/repos/Daily-Nerd/scout/issues/7"
    assert payload["state"] == "closed"
    assert payload["labels"] == ["scout:candidate", RETRACTED_LABEL]
    # existing labels are preserved and the retracted label is not doubled
    assert session.patches[1][1]["labels"] == ["scout:candidate", "bug", RETRACTED_LABEL]
    # one write every request_interval_seconds after the first, including
    # before the explainer and the pin
    assert len(sleeps) == 3
    assert all(wait > 1.9 for wait in sleeps)
    assert session.label_posts == 0


def test_apply_creates_retracted_label_when_missing(tmp_path):
    session = FakeSession(issues=[open_candidate(7)])
    result = retract(
        load_config(tmp_path), apply=True, token="t", session=session,
        sleep=lambda s: None,
    )
    assert session.label_posts == 1
    assert result.explainer_number == 9001


def test_apply_files_and_pins_explainer(tmp_path):
    session = FakeSession(issues=[open_candidate(7)], label_exists=True)
    result = retract(
        load_config(tmp_path), apply=True, token="t", session=session,
        sleep=lambda s: None,
    )
    url, payload = session.posts[0]
    assert url == "https://api.github.com/repos/Daily-Nerd/scout/issues"
    assert payload["title"] == EXPLAINER_TITLE
    assert payload["body"] == EXPLAINER_BODY
    assert payload["labels"] == [RETRACTED_LABEL]
    assert "\u2014" not in EXPLAINER_BODY
    assert session.puts == [
        "https://api.github.com/repos/Daily-Nerd/scout/issues/9001/pin"
    ]
    assert result.pinned


def test_pin_failure_still_leaves_explainer_open(tmp_path):
    session = FakeSession(issues=[open_candidate(7)], label_exists=True,
                          pin_status=403)
    result = retract(
        load_config(tmp_path), apply=True, token="t", session=session,
        sleep=lambda s: None,
    )
    assert not result.pinned
    assert result.explainer_number == 9001


def test_apply_without_token_fails_clearly(tmp_path):
    with pytest.raises(TokenMissingError, match="SCOUT_GITHUB_TOKEN"):
        retract(load_config(tmp_path), apply=True, token=None,
                session=FakeSession())


def test_explainer_clarifies_retraction_is_not_an_atlas_verdict():
    normalized = " ".join(EXPLAINER_BODY.split())
    assert (
        "A retracted issue means scout withdrew its own filing. It does "
        "not mean the atlas looked at the project or rejected it."
    ) in normalized
    assert "\u2014" not in EXPLAINER_BODY


def test_apply_marks_repo_retracted_in_store_from_title(tmp_path):
    from scout.store import Store

    history_path = tmp_path / "data" / "candidates.jsonl"
    store = Store(tmp_path / "state.db", history_path)
    session = FakeSession(issues=[open_candidate(7)], label_exists=True)
    result = retract(
        load_config(tmp_path), apply=True, token="t", session=session,
        sleep=lambda s: None, store=store,
    )
    store.close()

    assert result.closed == 1
    restored = History(history_path)
    assert restored.repos["owner/repo7"]["discovery"] == "retracted"


def test_apply_marks_repo_retracted_via_history_fallback_when_title_lacks_repo(
    tmp_path,
):
    from scout.store import Store

    history_path = tmp_path / "data" / "candidates.jsonl"
    store = Store(tmp_path / "state.db", history_path)
    store.history.mark_issue("owner/legacy", 7)

    session = FakeSession(
        issues=[{
            "number": 7,
            "title": "an issue with no candidate prefix",
            "labels": [{"name": "scout:candidate"}],
        }],
        label_exists=True,
    )
    result = retract(
        load_config(tmp_path), apply=True, token="t", session=session,
        sleep=lambda s: None, store=store,
    )
    store.close()

    assert result.closed == 1
    restored = History(history_path)
    assert restored.repos["owner/legacy"]["discovery"] == "retracted"


def test_dry_run_never_marks_the_store(tmp_path):
    from scout.store import Store

    history_path = tmp_path / "data" / "candidates.jsonl"
    store = Store(tmp_path / "state.db", history_path)
    session = FakeSession(issues=[open_candidate(7)])
    retract(
        load_config(tmp_path), apply=False, token=None, session=session,
        store=store,
    )
    store.close()

    restored = History(history_path)
    assert restored.repos == {}
