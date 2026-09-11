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


def test_title_only_rows_are_flagged_and_persisted(tmp_path):
    path = tmp_path / "candidates.jsonl"
    row = {"kind": "repo", "repo": "owner/junk", "issue_number": 12,
           "status": "filed", "sources": []}
    history = History(path)
    history.repos["owner/junk"] = row
    history.record_candidates([candidate()])
    history.write()

    restored = History(path)
    assert restored.mark_title_only_rows() == 1
    restored.write()

    repaired = History(path)
    assert repaired.repos["owner/junk"]["title_only"] is True
    assert "title_only" not in repaired.repos["alice/memorymesh"]
    assert repaired.is_repo_seen("owner/junk")  # stays part of the ledger


def test_title_only_marking_is_idempotent(tmp_path):
    path = tmp_path / "candidates.jsonl"
    history = History(path)
    history.repos["owner/junk"] = {"kind": "repo", "repo": "owner/junk"}
    assert history.mark_title_only_rows() == 1
    assert history.mark_title_only_rows() == 0


def test_readme_size_round_trips_through_history(tmp_path):
    path = tmp_path / "candidates.jsonl"
    history = History(path)
    history.record_candidates([candidate(), candidate("owner/noreadme")])
    history.update_readme_size("alice/memorymesh", 4321)
    history.update_readme_size("owner/noreadme", None)
    history.write()

    restored = History(path)
    assert restored.readme_size("alice/memorymesh") == 4321
    assert restored.has_readme_size("alice/memorymesh")
    assert restored.readme_size("owner/noreadme") is None
    assert restored.has_readme_size("owner/noreadme")
    assert not restored.has_readme_size("never/seen")


def test_pending_candidates_tolerate_extra_payload_keys(tmp_path):
    """Cached metadata such as readme_bytes must not break reconstruction."""
    path = tmp_path / "candidates.jsonl"
    history = History(path)
    history.record_candidates([candidate()])
    history.update_readme_size("alice/memorymesh", 4321)
    history.write()

    restored = History(path)
    pending = restored.pending_candidates()
    assert [item.repo for item in pending] == ["alice/memorymesh"]
