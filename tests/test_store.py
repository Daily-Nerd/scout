from scout.store import Store


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


def test_history_survives_a_new_runner_store(tmp_path):
    state = tmp_path / "state.db"
    history = tmp_path / "data" / "candidates.jsonl"
    with Store(state, history) as store:
        store.mark_post_seen("t3_abc", "reddit")
        store.mark_repo_seen("owner/repo", "github")

    with Store(tmp_path / "fresh-run.db", history) as store:
        assert store.is_post_seen("t3_abc")
        assert store.is_repo_seen("owner/repo")
