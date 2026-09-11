from __future__ import annotations

from pathlib import Path

from scout.config import Config, load
from scout.reddit import matching_terms, parse_feed, scan
from scout.store import Store

FIXTURES = Path(__file__).parent / "fixtures" / "reddit"

DEFAULT_CONFIG = """
[github]
queries = []
window_days = 14

[reddit]
subreddits = ["AIMemory", "mcp"]
request_interval_seconds = 2
contact_url = "https://github.com/Daily-Nerd/scout"

[terms]
agentish = ["agent", "mcp", "assistant"]
memoryish = ["memory", "recall", "checkpoint", "context"]

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


def load_config(tmp_path, subreddits=None) -> Config:
    text = DEFAULT_CONFIG
    if subreddits is not None:
        subs = ", ".join(f'"{s}"' for s in subreddits)
        text = text.replace('subreddits = ["AIMemory", "mcp"]',
                            f"subreddits = [{subs}]")
    path = tmp_path / "scout.toml"
    path.write_text(text)
    return load(path)


class FakeResponse:
    def __init__(self, text="", status_code=200):
        self.text = text
        self.status_code = status_code


class FakeSession:
    def __init__(self, pages: dict[str, FakeResponse]):
        self.pages = pages
        self.calls: list[str] = []

    def get(self, url, headers=None, timeout=None):
        self.calls.append(url)
        return self.pages[url]


def atom_session() -> FakeSession:
    return FakeSession({
        "https://www.reddit.com/r/AIMemory/new.rss":
            FakeResponse((FIXTURES / "aimemory.atom").read_text()),
        "https://www.reddit.com/r/mcp/new.rss":
            FakeResponse((FIXTURES / "mcp.rss").read_text()),
    })


def test_parse_feed_atom_entries():
    entries = parse_feed((FIXTURES / "aimemory.atom").read_text())
    assert [e.post_id for e in entries] == ["t3_abc111", "t3_abc222", "t3_abc333"]
    assert entries[0].author == "/u/alice_dev"
    assert entries[0].link == "https://www.reddit.com/r/AIMemory/comments/abc111/memorymesh/"
    assert entries[0].updated == "2026-09-08T12:00:00+00:00"


def test_parse_feed_rss2_items():
    entries = parse_feed((FIXTURES / "mcp.rss").read_text())
    assert len(entries) == 1
    entry = entries[0]
    assert entry.title.startswith("RecallSled")
    assert entry.author == "/u/carol_c"
    assert entry.post_id == entry.link
    assert entry.updated == "Thu, 09 Sep 2026 09:30:00 +0000"


def test_extract_repo_urls_strips_punctuation():
    from scout.reddit import extract_repo_urls

    text = 'See github.com/alice/memorymesh, and https://github.com/bob/context-store. Also github.com/carol/recall-sled.git!'
    assert extract_repo_urls(text) == [
        "alice/memorymesh",
        "bob/context-store",
        "carol/recall-sled",
    ]


def test_matching_terms_requires_one_of_each_list(tmp_path):
    config = load_config(tmp_path)
    assert matching_terms(config, "an agent with memory") == ["agent", "memory"]
    assert matching_terms(config, "agents only, nothing else") is None
    assert matching_terms(config, "memory and recall for my database") is None
    assert matching_terms(config, "MCP server with Recall") == ["mcp", "recall"]


def test_scan_collects_candidates_and_marks_posts(tmp_path):
    session = atom_session()
    with Store(tmp_path / "state.db") as store:
        candidates = scan(load_config(tmp_path), store,
                          session=session, sleep=lambda s: None)
    repos = [c.repo for c in candidates]
    assert repos == ["alice/memorymesh", "bob/context-store", "carol/recall-sled"]
    first = candidates[0]
    assert first.source == "reddit"
    assert first.source_url.endswith("/r/AIMemory/comments/abc111/memorymesh/")
    assert first.subreddit == "AIMemory"
    assert first.author == "/u/alice_dev"
    assert first.posted_at == "2026-09-08T12:00:00+00:00"
    assert first.html_url == "https://github.com/alice/memorymesh"
    assert "agent" in first.matched_terms and "memory" in first.matched_terms
    with Store(tmp_path / "state.db") as store:
        assert store.is_post_seen("t3_abc111")
        assert store.is_post_seen("t3_abc222")


def test_rerun_only_returns_new_posts(tmp_path):
    with Store(tmp_path / "state.db") as store:
        cfg = load_config(tmp_path)
        first = scan(cfg, store, session=atom_session(), sleep=lambda s: None)
        second = scan(cfg, store, session=atom_session(), sleep=lambda s: None)
    assert len(first) == 3
    assert second == []


def test_sends_descriptive_user_agent_and_spaces_requests(tmp_path):
    session = atom_session()
    sleeps: list[float] = []
    with Store(tmp_path / "state.db") as store:
        scan(load_config(tmp_path), store, session=session, sleep=sleeps.append)
    assert len(session.calls) == 2
    # one pause between the two subreddit requests
    assert len(sleeps) == 1
    assert 0 < sleeps[0] <= 2.0


def test_backs_off_on_403_and_returns_what_was_collected(tmp_path, capsys):
    session = FakeSession({
        "https://www.reddit.com/r/AIMemory/new.rss":
            FakeResponse((FIXTURES / "aimemory.atom").read_text()),
        "https://www.reddit.com/r/mcp/new.rss": FakeResponse(status_code=403),
    })
    with Store(tmp_path / "state.db") as store:
        candidates = scan(load_config(tmp_path), store,
                          session=session, sleep=lambda s: None)
    assert len(candidates) == 2
    # The throttled subreddit is retried three times, then the source stops.
    assert len(session.calls) == 5
    err = capsys.readouterr().err
    assert "403" in err and "backing off" in err


def test_backs_off_on_429_without_prior_requests(tmp_path, capsys):
    session = FakeSession({
        "https://www.reddit.com/r/AIMemory/new.rss": FakeResponse(status_code=429),
    })
    with Store(tmp_path / "state.db") as store:
        candidates = scan(load_config(tmp_path, subreddits=["AIMemory"]),
                          store, session=session, sleep=lambda s: None)
    assert candidates == []
    assert "429" in capsys.readouterr().err


def test_other_errors_skip_subreddit_but_keep_going(tmp_path, capsys):
    session = FakeSession({
        "https://www.reddit.com/r/AIMemory/new.rss": FakeResponse(status_code=404),
        "https://www.reddit.com/r/mcp/new.rss":
            FakeResponse((FIXTURES / "mcp.rss").read_text()),
    })
    with Store(tmp_path / "state.db") as store:
        candidates = scan(load_config(tmp_path), store,
                          session=session, sleep=lambda s: None)
    assert [c.repo for c in candidates] == ["carol/recall-sled"]
    assert "404" in capsys.readouterr().err
