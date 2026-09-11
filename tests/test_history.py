from __future__ import annotations

import json

from scout.history import History
from scout.models import Candidate


def candidate(repo: str = "alice/memorymesh") -> Candidate:
    return Candidate(
        repo=repo,
        source="github",
        source_url=f"https://github.com/{repo}",
        matched_terms=["topic:agent-memory"],
    )


def test_history_round_trips_repos_posts_and_issue(tmp_path):
    path = tmp_path / "data" / "candidates.jsonl"
    history = History(path)
    history.mark_post_seen("t3_abc", "reddit")
    history.record_candidates([candidate()])
    history.mark_issue("alice/memorymesh", 42)
    history.write()

    restored = History(path)
    assert restored.is_post_seen("t3_abc")
    assert restored.is_repo_seen("alice/memorymesh")
    assert restored.repos["alice/memorymesh"]["issue_number"] == 42
    assert restored.repos["alice/memorymesh"]["latest"]["repo"] == "alice/memorymesh"


def test_history_ignores_malformed_lines(tmp_path):
    path = tmp_path / "candidates.jsonl"
    path.write_text("not json\n" + json.dumps({"kind": "unknown"}) + "\n")
    history = History(path)
    assert history.repos == {}
    assert history.posts == {}


def test_pending_candidates_are_available_without_rediscovery(tmp_path):
    path = tmp_path / "candidates.jsonl"
    history = History(path)
    history.record_candidates([candidate()])
    history.write()

    restored = History(path)
    pending = restored.pending_candidates()
    assert [item.repo for item in pending] == ["alice/memorymesh"]


def test_issue_migration_marker_round_trips(tmp_path):
    path = tmp_path / "candidates.jsonl"
    history = History(path)
    history.mark_issues_migrated()
    history.write()
    assert History(path).issues_migrated
