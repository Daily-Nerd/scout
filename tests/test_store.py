from scout.store import Store
from scout.tiering import Component, TierScore


def tier_score(repo: str = "owner/repo") -> TierScore:
    return TierScore(
        repo=repo,
        score=7.5,
        tier="b",
        label="scout:tier-b",
        components={"stars": Component(input=10, value=7.5)},
        absent=[],
        scored_at="2026-09-11T00:00:00Z",
    )


def test_posts_round_trip(tmp_path):
    with Store(tmp_path / "state.db") as store:
        assert not store.is_post_seen("t3_abc")
        store.mark_post_seen("t3_abc", "reddit")
        assert store.is_post_seen("t3_abc")


def test_repos_round_trip(tmp_path):
    with Store(tmp_path / "state.db") as store:
        assert not store.is_repo_seen("owner/repo")
        store.mark_repo_seen("owner/repo", "github")
        assert store.is_repo_seen("owner/repo")


def test_marks_are_idempotent(tmp_path):
    with Store(tmp_path / "state.db") as store:
        store.mark_post_seen("t3_abc", "reddit")
        store.mark_post_seen("t3_abc", "reddit")
        store.mark_repo_seen("owner/repo", "github")
        store.mark_repo_seen("owner/repo", "github")
        assert store.is_post_seen("t3_abc")
        assert store.is_repo_seen("owner/repo")


def test_filed_issues(tmp_path):
    with Store(tmp_path / "state.db") as store:
        assert not store.is_filed("owner/repo")
        assert store.filed_issue_number("owner/repo") is None
        store.mark_filed("owner/repo", 42)
        assert store.is_filed("owner/repo")
        assert store.filed_issue_number("owner/repo") == 42


def test_score_and_known_proxies_reach_history(tmp_path):
    history_path = tmp_path / "data" / "candidates.jsonl"
    with Store(tmp_path / "state.db", history_path) as store:
        store.record_score("owner/repo", tier_score())
        store.mark_known("owner/known")
        store.mark_retracted("owner/retracted")

    with Store(tmp_path / "fresh.db", history_path) as store:
        assert store.history.repos["owner/repo"]["tier"] == "b"
        assert store.history.repos["owner/known"]["assessment"] == "atlas-known"
        assert store.history.repos["owner/retracted"]["discovery"] == "retracted"


def test_score_and_known_proxies_are_no_ops_without_history(tmp_path):
    with Store(tmp_path / "state.db") as store:
        store.record_score("owner/repo", tier_score())
        store.mark_known("owner/known")
        store.mark_retracted("owner/retracted")


def test_repair_rows_proxy(tmp_path):
    history_path = tmp_path / "data" / "candidates.jsonl"
    with Store(tmp_path / "state.db", history_path) as store:
        store.mark_repo_seen("owner/repo", "github")
        # simulate a pre-existing row that predates discovery/assessment
        del store.history.repos["owner/repo"]["discovery"]
        counts = store.repair_rows()
        assert counts["discovery"] == 1


def test_repair_rows_proxy_without_history_returns_empty(tmp_path):
    with Store(tmp_path / "state.db") as store:
        assert store.repair_rows() == {}


def test_rows_due_for_refresh_proxy(tmp_path):
    from datetime import UTC, datetime

    history_path = tmp_path / "data" / "candidates.jsonl"
    with Store(tmp_path / "state.db", history_path) as store:
        store.mark_repo_seen("owner/repo", "github")
        due = store.rows_due_for_refresh(datetime(2026, 9, 11, tzinfo=UTC), 14, 50)
        assert due == ["owner/repo"]


def test_rows_due_for_refresh_proxy_without_history_returns_empty(tmp_path):
    from datetime import UTC, datetime

    with Store(tmp_path / "state.db") as store:
        assert store.rows_due_for_refresh(datetime(2026, 9, 11, tzinfo=UTC), 14, 50) == []


def test_update_latest_proxy(tmp_path):
    history_path = tmp_path / "data" / "candidates.jsonl"
    with Store(tmp_path / "state.db", history_path) as store:
        store.mark_repo_seen("owner/repo", "github")
        store.update_latest("owner/repo", stars=42)
        assert store.history.repos["owner/repo"]["latest"]["stars"] == 42


def test_update_latest_proxy_is_a_no_op_without_history(tmp_path):
    with Store(tmp_path / "state.db") as store:
        store.update_latest("owner/repo", stars=42)


def test_mark_refresh_attempt_proxy(tmp_path):
    history_path = tmp_path / "data" / "candidates.jsonl"
    with Store(tmp_path / "state.db", history_path) as store:
        store.mark_repo_seen("owner/repo", "github")
        store.mark_refresh_attempt("owner/repo", "2026-09-11T00:00:00Z", error="404")
        row = store.history.repos["owner/repo"]
        assert row["refresh_attempted_at"] == "2026-09-11T00:00:00Z"
        assert row["refresh_error"] == "404"


def test_mark_refresh_attempt_proxy_is_a_no_op_without_history(tmp_path):
    with Store(tmp_path / "state.db") as store:
        store.mark_refresh_attempt("owner/repo", "2026-09-11T00:00:00Z", error="404")


def test_history_survives_a_new_runner_store(tmp_path):
    state = tmp_path / "state.db"
    history = tmp_path / "data" / "candidates.jsonl"
    with Store(state, history) as store:
        store.mark_post_seen("t3_abc", "reddit")
        store.mark_repo_seen("owner/repo", "github")

    with Store(tmp_path / "fresh-run.db", history) as store:
        assert store.is_post_seen("t3_abc")
        assert store.is_repo_seen("owner/repo")
