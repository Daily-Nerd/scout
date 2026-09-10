from pathlib import Path

import pytest

from scout.config import load

REPO_CONFIG = Path(__file__).resolve().parent.parent / "scout.toml"


def test_repo_config_loads():
    config = load(REPO_CONFIG)
    assert config.github.queries, "default github queries must not be empty"
    assert config.github.window_days == 14
    assert len(config.reddit.subreddits) == 6
    assert "AIMemory" in config.reddit.subreddits
    assert config.reddit.request_interval_seconds >= 2
    assert config.terms.agentish
    assert config.terms.memoryish
    assert config.atlas.repo == "neoneye/agent-memory-atlas"
    assert config.issues.target_repo == "Daily-Nerd/scout"
    assert config.issues.label == "scout:candidate"


def test_terms_match_is_case_insensitive():
    config = load(REPO_CONFIG)
    matched = config.terms.match("An Agent with Long-Term Memory")
    assert "agent" in matched
    assert "long-term" in matched


def test_missing_table_uses_defaults(tmp_path):
    path = tmp_path / "scout.toml"
    path.write_text("[github]\nqueries = [\"topic:x\"]\n")
    config = load(path)
    assert config.github.queries == ["topic:x"]
    assert config.reddit.subreddits == []
    assert config.atlas.archive_org == "agent-memory-atlas-archive"


def test_non_table_section_raises(tmp_path):
    path = tmp_path / "scout.toml"
    path.write_text('github = "nope"\n')
    with pytest.raises(ValueError):
        load(path)
