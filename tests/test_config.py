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
    assert config.state.candidates_path == Path("data/candidates.jsonl")
    assert config.state.reports_path == Path("reports")
    assert config.tiering.stars_cap == 500
    assert config.tiering.tier_a_min == 8.0
    assert config.tiering.tier_b_min == 5.0
    assert config.tiering.tests_weight == 2.0
    assert config.tiering.source_weight == 1.5
    assert config.tiering.source_cap == 40
    assert ".py" in config.tiering.source_extensions
    assert ".ts" in config.tiering.source_extensions
    assert config.tiering.list_penalty_weight == 3.0
    assert "awesome" in config.tiering.list_words
    assert config.tiering.tree_max_per_run == 300
    assert config.filing.max_per_run == 25


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
    assert config.tiering.tier_a_min == 8.0
    assert config.state.reports_path == Path("reports")
    assert config.tiering.tests_weight == 2.0
    assert config.tiering.source_weight == 1.5
    assert config.tiering.source_cap == 40
    assert config.tiering.source_extensions == [
        ".py", ".ts", ".tsx", ".js", ".go", ".rs", ".java", ".kt",
        ".rb", ".cs", ".cpp", ".c", ".swift",
    ]
    assert config.tiering.list_penalty_weight == 3.0
    assert config.tiering.list_words == [
        "awesome", "list", "curated", "collection", "resources", "roundup",
    ]
    assert config.tiering.tree_max_per_run == 300
    assert config.filing.max_per_run == 25


def test_non_table_section_raises(tmp_path):
    path = tmp_path / "scout.toml"
    path.write_text('github = "nope"\n')
    with pytest.raises(ValueError):
        load(path)


def test_filing_section_overrides_max_per_run(tmp_path):
    path = tmp_path / "scout.toml"
    path.write_text("[filing]\nmax_per_run = 10\n")
    config = load(path)
    assert config.filing.max_per_run == 10
