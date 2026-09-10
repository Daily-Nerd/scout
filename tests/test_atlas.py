from __future__ import annotations

import io
import json
import tarfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

from scout import atlas
from scout.atlas import (
    REASON_ARCHIVE,
    REASON_ATLAS,
    REASON_ISSUE,
    AtlasSet,
    load_atlas,
    parse_frontmatter,
    parse_systems_index,
    parse_tarball,
)
from scout.config import Config, load

FIXTURES = Path(__file__).parent / "fixtures" / "atlas"

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


def build_tarball(fixtures: Path) -> bytes:
    """Pack the fixture tree the way codeload would: one top-level dir."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for file in sorted(fixtures.rglob("*")):
            if file.is_file():
                arcname = Path("agent-memory-atlas-main") / file.relative_to(fixtures)
                tar.add(file, arcname=str(arcname))
    return buffer.getvalue()


class FakeResponse:
    def __init__(self, payload=None, content: bytes = b"", status_code=200):
        self._payload = payload
        self.content = content
        self.status_code = status_code

    def json(self):
        return self._payload

    def raise_for_status(self):
        return None


class FakeSession:
    """Routes the archive org and issue search calls."""

    def __init__(self, fail_on_call: bool = False):
        self.fail_on_call = fail_on_call
        self.calls: list[str] = []

    def get(self, url, params=None, headers=None, timeout=None):
        if self.fail_on_call:
            raise AssertionError("network call made despite fresh cache")
        self.calls.append(url)
        if "/orgs/agent-memory-atlas-archive/repos" in url:
            return FakeResponse(payload=[
                {"name": "someorg--contextkeeper"},
                {"name": "legacy--old-mem"},
                {"name": "not-a-fork-name"},
            ])
        if "/search/issues" in url:
            assert headers.get("Authorization") == "Bearer t"
            return FakeResponse(payload={"items": [
                {"title": "candidate: filed/already"},
                {"title": "candidate: Another/One"},
                {"title": "unrelated discussion"},
            ]})
        raise AssertionError(f"unexpected url: {url}")


def test_parse_frontmatter_basic_and_quoted():
    text = '---\nsource_name: RecallMirror\nsource_url: "https://github.com/o/r"\n---\nbody\n'
    assert parse_frontmatter(text) == {
        "source_name": "RecallMirror",
        "source_url": "https://github.com/o/r",
    }
    assert parse_frontmatter("no frontmatter here") == {}


def test_parse_systems_index_normalises_and_skips_garbage():
    repos = parse_systems_index(
        '<code class="az-repo">SomeOrg/ContextKeeper</code>'
        '<code class="az-repo">just a label</code>'
    )
    assert repos == {"someorg/contextkeeper"}


def test_parse_tarball_reads_frontmatter_and_index():
    repos = parse_tarball(build_tarball(FIXTURES))
    assert repos == {
        "daily-nerd/recallmirror",
        "someorg/contextkeeper",
        "thirdparty/index-only",
    }


def test_load_atlas_merges_all_three_sources(tmp_path):
    session = FakeSession()
    result = load_atlas(
        load_config(tmp_path), token="t", session=session,
        fetch_tarball=lambda: build_tarball(FIXTURES),
    )
    assert result.is_known("Daily-Nerd/RecallMirror") == REASON_ATLAS
    assert result.is_known("someorg/contextkeeper") == REASON_ATLAS
    assert result.is_known("thirdparty/index-only") == REASON_ATLAS
    assert result.is_known("legacy/old-mem") == REASON_ARCHIVE
    assert result.is_known("filed/already") == REASON_ISSUE
    assert result.is_known("unknown/repo") is None
    assert "someorg/contextkeeper" in result.repos


def test_fresh_cache_is_used_without_network(tmp_path):
    cfg = load_config(tmp_path)
    first = load_atlas(cfg, token="t", session=FakeSession(),
                       fetch_tarball=lambda: build_tarball(FIXTURES))
    second = load_atlas(cfg, token="t", session=FakeSession(fail_on_call=True),
                        fetch_tarball=lambda: (_ for _ in ()).throw(
                            AssertionError("tarball refetched despite fresh cache")))
    assert second.repos == first.repos


def test_stale_cache_refreshes(tmp_path):
    cfg = load_config(tmp_path)
    cache = cfg.state.db_path.parent / "atlas-cache.json"
    cache.parent.mkdir(parents=True, exist_ok=True)
    stale = {
        "fetched_at": (datetime.now(UTC) - timedelta(hours=25)).isoformat(),
        "repos": [["stale/repo", REASON_ATLAS]],
    }
    cache.write_text(json.dumps(stale))
    result = load_atlas(cfg, token="t", session=FakeSession(),
                        fetch_tarball=lambda: build_tarball(FIXTURES))
    assert result.is_known("stale/repo") is None
    assert result.is_known("daily-nerd/recallmirror") == REASON_ATLAS


def test_missing_token_skips_issue_check(tmp_path, capsys):
    session = FakeSession()
    result = load_atlas(load_config(tmp_path), token=None, session=session,
                        fetch_tarball=lambda: build_tarball(FIXTURES))
    assert not any("/search/issues" in url for url in session.calls)
    assert result.is_known("filed/already") is None
    assert "no token" in capsys.readouterr().err


def test_atlas_set_round_trips_through_pairs():
    original = AtlasSet(reasons={"o/r": REASON_ATLAS, "a/b": REASON_ISSUE})
    restored = AtlasSet.from_pairs(original.to_pairs())
    assert restored.repos == original.repos
    assert restored.is_known("O/R") == REASON_ATLAS
