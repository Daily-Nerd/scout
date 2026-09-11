from __future__ import annotations

import json
from datetime import UTC, datetime

from scout.history import History
from scout.models import Candidate
from scout.tiering import Component, TierScore

NOW = datetime(2026, 9, 11, tzinfo=UTC)


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


def test_tree_signals_round_trip_through_history(tmp_path):
    path = tmp_path / "candidates.jsonl"
    history = History(path)
    history.record_candidates([candidate(), candidate("owner/notested")])
    history.update_tree_signals("alice/memorymesh", True, 12, "2026-09-11T00:00:00Z")
    history.update_tree_signals("owner/notested", False, 0, "2026-09-11T00:00:00Z")
    history.write()

    restored = History(path)
    assert restored.tree_signals("alice/memorymesh") == (True, 12)
    assert restored.has_tree_signals("alice/memorymesh")
    assert restored.tree_signals("owner/notested") == (False, 0)
    assert restored.has_tree_signals("owner/notested")
    assert not restored.has_tree_signals("never/seen")
    assert restored.tree_signals("never/seen") is None


def tier_score(repo: str = "alice/memorymesh") -> TierScore:
    return TierScore(
        repo=repo,
        score=9.123456,
        tier="a",
        label="scout:tier-a",
        components={
            "stars": Component(input=120, value=6.123456),
            "topics": Component(input=["agent-memory"], value=3.0),
            "readme": Component(input=None, value=None),
        },
        absent=["readme"],
        scored_at="2026-09-11T00:00:00Z",
    )


def test_record_score_round_trips_through_write_and_load(tmp_path):
    path = tmp_path / "candidates.jsonl"
    history = History(path)
    history.mark_repo_seen("alice/memorymesh", "github")
    history.record_score("alice/memorymesh", tier_score())
    history.write()

    restored = History(path)
    row = restored.repos["alice/memorymesh"]
    assert row["score"] == 9.1235
    assert row["tier"] == "a"
    assert row["assessment"] == "tier-a"
    assert row["scored_at"] == "2026-09-11T00:00:00Z"
    assert row["absent_components"] == ["readme"]
    assert row["components"]["stars"] == {"input": 120, "value": 6.1235}
    assert row["components"]["topics"] == {"input": ["agent-memory"], "value": 3.0}
    assert row["components"]["readme"] == {"input": None, "value": None}


def test_rows_created_by_any_writer_start_pending_and_seen(tmp_path):
    """Whichever method first touches a repo, the row it creates carries
    the same defaults as one written by mark_repo_seen."""
    path = tmp_path / "candidates.jsonl"
    history = History(path)
    history.record_score("owner/scored", tier_score("owner/scored"))
    history.mark_known("owner/known")
    history.mark_retracted("owner/retracted")
    history.mark_refresh_attempt("owner/attempted", "2026-09-11T00:00:00Z")
    history.update_latest("owner/latest", stars=1)
    history.write()

    restored = History(path)
    for repo in ("owner/scored", "owner/known", "owner/attempted", "owner/latest"):
        row = restored.repos[repo]
        assert row["status"] == "pending", repo
        assert row["discovery"] == "seen", repo
        assert row["sources"] == [], repo
        assert row["first_seen_at"], repo
    # mark_retracted overrides the discovery default it just created
    assert restored.repos["owner/retracted"]["status"] == "pending"
    assert restored.repos["owner/retracted"]["discovery"] == "retracted"


def test_mark_known_sets_assessment_without_scoring(tmp_path):
    path = tmp_path / "candidates.jsonl"
    history = History(path)
    history.mark_repo_seen("owner/known", "github")
    history.mark_known("owner/known")
    history.write()

    restored = History(path)
    row = restored.repos["owner/known"]
    assert row["assessment"] == "atlas-known"
    assert "score" not in row


def test_mark_repo_seen_sets_discovery_only_on_creation(tmp_path):
    path = tmp_path / "candidates.jsonl"
    history = History(path)
    history.mark_repo_seen("alice/memorymesh", "github")
    assert history.repos["alice/memorymesh"]["discovery"] == "seen"

    history.mark_issue("alice/memorymesh", 42)
    assert history.repos["alice/memorymesh"]["discovery"] == "filed"

    # a rescan must not downgrade discovery back to "seen"
    history.mark_repo_seen("alice/memorymesh", "github")
    assert history.repos["alice/memorymesh"]["discovery"] == "filed"


def test_mark_title_only_rows_sets_discovery_only_without_issue_number(tmp_path):
    path = tmp_path / "candidates.jsonl"
    history = History(path)
    history.repos["owner/junk"] = {"kind": "repo", "repo": "owner/junk"}
    history.repos["owner/filed-junk"] = {
        "kind": "repo", "repo": "owner/filed-junk", "issue_number": 5,
        "discovery": "filed",
    }
    history.mark_title_only_rows()
    assert history.repos["owner/junk"]["discovery"] == "title_only"
    # already has an issue, so discovery stays "filed", not "title_only"
    assert history.repos["owner/filed-junk"]["discovery"] == "filed"


def test_mark_retracted_sets_discovery(tmp_path):
    path = tmp_path / "candidates.jsonl"
    history = History(path)
    history.mark_issue("owner/repo", 9)
    history.mark_retracted("owner/repo")
    history.write()

    restored = History(path)
    assert restored.repos["owner/repo"]["discovery"] == "retracted"


def test_repair_rows_backfills_missing_fields_without_touching_existing(tmp_path):
    path = tmp_path / "candidates.jsonl"
    history = History(path)
    history.repos["owner/pending"] = {"kind": "repo", "repo": "owner/pending"}
    history.repos["owner/retracted-candidate"] = {
        "kind": "repo", "repo": "owner/retracted-candidate", "issue_number": 12,
    }
    history.repos["owner/still-filed"] = {
        "kind": "repo", "repo": "owner/still-filed", "issue_number": 200,
    }
    history.repos["owner/title-only"] = {
        "kind": "repo", "repo": "owner/title-only", "title_only": True,
        "issue_number": 3,
    }
    history.repos["owner/no-readme"] = {
        "kind": "repo", "repo": "owner/no-readme",
        "latest": {"repo": "owner/no-readme", "readme_bytes": None},
    }
    history.repos["owner/already-tagged"] = {
        "kind": "repo", "repo": "owner/already-tagged",
        "discovery": "seen", "assessment": "tier-b",
    }

    counts = history.repair_rows(retracted_through=100)

    assert history.repos["owner/pending"]["discovery"] == "seen"
    assert history.repos["owner/retracted-candidate"]["discovery"] == "retracted"
    assert history.repos["owner/still-filed"]["discovery"] == "filed"
    # title_only and issue_number 3 <= 100: the retraction wins, the flag
    # only says the row was rebuilt from a title
    assert history.repos["owner/title-only"]["discovery"] == "retracted"
    assert history.repos["owner/no-readme"]["latest"]["readme_bytes"] == 0
    assert history.repos["owner/pending"]["assessment"] == "none"
    assert history.repos["owner/already-tagged"]["discovery"] == "seen"
    assert history.repos["owner/already-tagged"]["assessment"] == "tier-b"
    assert counts == {"discovery": 5, "assessment": 5, "readme_bytes": 1}


def test_repair_rows_prefers_retracted_over_title_only_flag(tmp_path):
    """Every row filed before the retraction is also title_only (they were
    rebuilt from issue titles), so the flag must not shadow the issue
    number: retracted first, then filed, then title_only, then seen."""
    path = tmp_path / "candidates.jsonl"
    history = History(path)
    history.repos["owner/retracted-title"] = {
        "kind": "repo", "repo": "owner/retracted-title", "title_only": True,
        "issue_number": 3,
    }
    history.repos["owner/filed-title"] = {
        "kind": "repo", "repo": "owner/filed-title", "title_only": True,
        "issue_number": 300,
    }
    history.repos["owner/bare-title"] = {
        "kind": "repo", "repo": "owner/bare-title", "title_only": True,
    }
    history.repair_rows(retracted_through=100)
    assert history.repos["owner/retracted-title"]["discovery"] == "retracted"
    assert history.repos["owner/filed-title"]["discovery"] == "filed"
    assert history.repos["owner/bare-title"]["discovery"] == "title_only"


def test_repair_rows_without_retracted_through_leaves_issues_filed(tmp_path):
    path = tmp_path / "candidates.jsonl"
    history = History(path)
    history.repos["owner/old-issue"] = {
        "kind": "repo", "repo": "owner/old-issue", "issue_number": 1,
    }
    history.repair_rows(retracted_through=None)
    assert history.repos["owner/old-issue"]["discovery"] == "filed"


def test_repo_for_issue_reverse_lookup(tmp_path):
    path = tmp_path / "candidates.jsonl"
    history = History(path)
    history.mark_issue("owner/repo", 42)
    assert history.repo_for_issue(42) == "owner/repo"
    assert history.repo_for_issue(999) is None


def test_unknown_keys_on_loaded_row_survive_write(tmp_path):
    path = tmp_path / "candidates.jsonl"
    path.write_text(json.dumps({
        "kind": "repo", "repo": "owner/repo", "sources": [],
        "some_future_field": "keep me",
    }) + "\n")
    history = History(path)
    history.mark_repo_seen("owner/repo", "github")
    history.write()

    restored = History(path)
    assert restored.repos["owner/repo"]["some_future_field"] == "keep me"


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


def test_rows_due_for_refresh_orders_oldest_first_and_caps(tmp_path):
    path = tmp_path / "candidates.jsonl"
    history = History(path)
    history.repos["owner/never-scored"] = {
        "kind": "repo", "repo": "owner/never-scored", "discovery": "seen",
        "assessment": "none", "last_seen_at": "2026-09-01T00:00:00Z",
    }
    history.repos["owner/oldest-score"] = {
        "kind": "repo", "repo": "owner/oldest-score", "discovery": "filed",
        "assessment": "tier-b", "last_seen_at": "2026-08-01T00:00:00Z",
        "latest": {"repo": "owner/oldest-score", "fetched_at": "2026-08-01T00:00:00Z"},
    }
    history.repos["owner/mid-score"] = {
        "kind": "repo", "repo": "owner/mid-score", "discovery": "title_only",
        "assessment": "none", "last_seen_at": "2026-08-20T00:00:00Z",
        "latest": {"repo": "owner/mid-score", "fetched_at": "2026-08-20T00:00:00Z"},
    }
    history.repos["owner/fresh-score"] = {
        "kind": "repo", "repo": "owner/fresh-score", "discovery": "seen",
        "assessment": "tier-a", "last_seen_at": "2026-09-10T00:00:00Z",
        "latest": {"repo": "owner/fresh-score", "fetched_at": "2026-09-10T00:00:00Z"},
    }
    history.repos["owner/known"] = {
        "kind": "repo", "repo": "owner/known", "discovery": "seen",
        "assessment": "atlas-known", "last_seen_at": "2026-08-01T00:00:00Z",
    }
    history.repos["owner/retracted"] = {
        "kind": "repo", "repo": "owner/retracted", "discovery": "retracted",
        "assessment": "none", "last_seen_at": "2026-08-01T00:00:00Z",
    }

    due = history.rows_due_for_refresh(NOW, days=14, limit=50)
    assert due == ["owner/never-scored", "owner/oldest-score", "owner/mid-score"]

    capped = history.rows_due_for_refresh(NOW, days=14, limit=2)
    assert capped == ["owner/never-scored", "owner/oldest-score"]


def test_rows_due_for_refresh_ties_broken_by_last_seen_at(tmp_path):
    path = tmp_path / "candidates.jsonl"
    history = History(path)
    history.repos["owner/seen-later"] = {
        "kind": "repo", "repo": "owner/seen-later", "discovery": "seen",
        "assessment": "none", "last_seen_at": "2026-09-05T00:00:00Z",
    }
    history.repos["owner/seen-earlier"] = {
        "kind": "repo", "repo": "owner/seen-earlier", "discovery": "seen",
        "assessment": "none", "last_seen_at": "2026-09-01T00:00:00Z",
    }
    due = history.rows_due_for_refresh(NOW, days=14, limit=50)
    assert due == ["owner/seen-earlier", "owner/seen-later"]


def test_update_latest_merges_fields_into_existing_latest(tmp_path):
    path = tmp_path / "candidates.jsonl"
    history = History(path)
    history.record_candidates([candidate()])
    history.update_latest("alice/memorymesh", stars=99, pushed_at="2026-09-10T00:00:00Z")
    history.write()

    restored = History(path)
    latest = restored.repos["alice/memorymesh"]["latest"]
    assert latest["stars"] == 99
    assert latest["pushed_at"] == "2026-09-10T00:00:00Z"
    assert latest["repo"] == "alice/memorymesh"  # untouched fields survive


def test_mark_refresh_attempt_stamps_time_and_clears_error_on_success(tmp_path):
    path = tmp_path / "candidates.jsonl"
    history = History(path)
    history.mark_repo_seen("owner/repo", "github")
    history.mark_refresh_attempt("owner/repo", "2026-09-11T00:00:00Z", error="404")
    history.write()

    restored = History(path)
    row = restored.repos["owner/repo"]
    assert row["refresh_attempted_at"] == "2026-09-11T00:00:00Z"
    assert row["refresh_error"] == "404"

    restored.mark_refresh_attempt("owner/repo", "2026-09-12T00:00:00Z", error=None)
    restored.write()

    reloaded = History(path)
    row = reloaded.repos["owner/repo"]
    assert row["refresh_attempted_at"] == "2026-09-12T00:00:00Z"
    assert "refresh_error" not in row


def test_rows_due_for_refresh_uses_max_of_fetched_at_and_refresh_attempted_at(tmp_path):
    path = tmp_path / "candidates.jsonl"
    history = History(path)
    # never fetched, but a failed refresh attempt was stamped today: must
    # not be reselected until `days` pass again
    history.repos["owner/dead"] = {
        "kind": "repo", "repo": "owner/dead", "discovery": "seen",
        "assessment": "none", "refresh_attempted_at": "2026-09-11T00:00:00Z",
        "refresh_error": "404", "last_seen_at": "2026-08-01T00:00:00Z",
    }
    due = history.rows_due_for_refresh(NOW, days=14, limit=50)
    assert due == []


def test_rows_due_for_refresh_sorts_by_refresh_attempted_at_when_fetched_at_absent(tmp_path):
    path = tmp_path / "candidates.jsonl"
    history = History(path)
    history.repos["owner/attempted-old"] = {
        "kind": "repo", "repo": "owner/attempted-old", "discovery": "seen",
        "assessment": "none", "refresh_attempted_at": "2026-08-01T00:00:00Z",
        "last_seen_at": "2026-08-01T00:00:00Z",
    }
    history.repos["owner/never-touched"] = {
        "kind": "repo", "repo": "owner/never-touched", "discovery": "seen",
        "assessment": "none", "last_seen_at": "2026-08-15T00:00:00Z",
    }
    due = history.rows_due_for_refresh(NOW, days=14, limit=50)
    # never-touched has no stamp at all (sorts first, before any real
    # timestamp), attempted-old has an old refresh_attempted_at
    assert due == ["owner/never-touched", "owner/attempted-old"]


def test_rows_due_for_refresh_uses_the_later_of_fetched_at_and_refresh_attempted_at(
    tmp_path,
):
    path = tmp_path / "candidates.jsonl"
    history = History(path)
    # fetched long ago, but freshly attempted (e.g. a 404 this run): the
    # later stamp wins and keeps it out
    history.repos["owner/recently-attempted"] = {
        "kind": "repo", "repo": "owner/recently-attempted", "discovery": "filed",
        "assessment": "tier-b", "refresh_attempted_at": "2026-09-10T00:00:00Z",
        "last_seen_at": "2026-01-01T00:00:00Z",
        "latest": {"repo": "owner/recently-attempted",
                   "fetched_at": "2026-01-01T00:00:00Z"},
    }
    due = history.rows_due_for_refresh(NOW, days=14, limit=50)
    assert due == []


def test_rows_due_for_refresh_ignores_scored_at(tmp_path):
    """Scoring runs every run from the cached payload, so scored_at says
    nothing about how old the metadata is. A pending github row rescored
    this run from a payload fetched in August is still due; a row the
    search re-found this run (fetched_at = now) is not."""
    path = tmp_path / "candidates.jsonl"
    history = History(path)
    history.repos["owner/rescored-stale"] = {
        "kind": "repo", "repo": "owner/rescored-stale", "discovery": "seen",
        "assessment": "tier-b", "scored_at": "2026-09-11T00:00:00Z",
        "last_seen_at": "2026-08-01T00:00:00Z",
        "latest": {"repo": "owner/rescored-stale", "source": "github",
                   "fetched_at": "2026-08-01T00:00:00Z"},
    }
    history.record_candidates([candidate("owner/refound")])
    assert history.repos["owner/refound"]["latest"]["fetched_at"] \
        == history.repos["owner/refound"]["last_seen_at"]

    due = history.rows_due_for_refresh(datetime.now(UTC), days=14, limit=50)
    assert due == ["owner/rescored-stale"]


def test_record_candidates_stamps_fetched_at_only_for_github_hits(tmp_path):
    """A search hit carries real repo metadata; a Reddit hit carries only
    what the post said, so it must not count as a metadata fetch."""
    path = tmp_path / "candidates.jsonl"
    history = History(path)
    reddit = Candidate(
        repo="bob/memorymesh", source="reddit",
        source_url="https://www.reddit.com/r/AIMemory/comments/1/x/",
    )
    history.record_candidates([candidate(), reddit])
    assert "fetched_at" in history.repos["alice/memorymesh"]["latest"]
    assert "fetched_at" not in history.repos["bob/memorymesh"]["latest"]


def test_update_latest_always_stamps_fetched_at(tmp_path):
    path = tmp_path / "candidates.jsonl"
    history = History(path)
    history.record_candidates([candidate()])
    history.update_latest(
        "alice/memorymesh", stars=99, fetched_at="2026-09-11T00:00:00Z",
    )
    assert history.repos["alice/memorymesh"]["latest"]["fetched_at"] \
        == "2026-09-11T00:00:00Z"

    # without an explicit stamp, the call time is used
    before = datetime.now(UTC)
    history.update_latest("alice/memorymesh", stars=100)
    stamped = history.repos["alice/memorymesh"]["latest"]["fetched_at"]
    assert datetime.fromisoformat(stamped) >= before


def test_update_latest_creates_latest_for_a_title_only_row(tmp_path):
    path = tmp_path / "candidates.jsonl"
    history = History(path)
    history.repos["owner/title-only"] = {
        "kind": "repo", "repo": "owner/title-only", "discovery": "title_only",
        "title_only": True, "sources": ["github"],
    }
    history.update_latest("owner/title-only", source="github", stars=10)
    history.write()

    restored = History(path)
    latest = restored.repos["owner/title-only"]["latest"]
    assert latest["repo"] == "owner/title-only"
    assert latest["source"] == "github"
    assert latest["stars"] == 10
