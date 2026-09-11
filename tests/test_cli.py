from __future__ import annotations

import json
import os

import pytest

from scout import cli
from scout.atlas import AtlasSet, REASON_ARCHIVE, REASON_ATLAS, REASON_ISSUE
from scout.cli import main
from scout.models import Candidate

CONFIG_TEMPLATE = """
[github]
queries = ["topic:agent-memory"]
window_days = 14

[reddit]
subreddits = ["AIMemory"]
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

[tiering]
stars_weight = 2.0
stars_cap = 500
recency_weight = 4.0
recency_window_days = 90
name_weight = 2.0
readme_weight = 2.0
readme_cap_bytes = 20000
topic_weight = 1.0
density_weight = 3.0
tier_a_min = 8.0
tier_b_min = 5.0

[state]
db_path = "{db_path}"
candidates_path = "{history_path}"
reports_path = "{reports_path}"
"""


def write_config(tmp_path) -> object:
    path = tmp_path / "scout.toml"
    path.write_text(CONFIG_TEMPLATE.format(
        db_path=tmp_path / "state" / "scout.db",
        history_path=tmp_path / "data" / "candidates.jsonl",
        reports_path=tmp_path / "reports",
    ))
    return path


def github_candidate(repo: str = "new/hot") -> Candidate:
    return Candidate(
        repo=repo,
        source="github",
        source_url=f"https://github.com/{repo}",
        matched_terms=["topic:agent-memory"],
        description="agent memory",
        stars=5,
        pushed_at="2026-09-08T00:00:00Z",
        license="MIT",
        html_url=f"https://github.com/{repo}",
    )


@pytest.fixture
def wired(tmp_path, monkeypatch):
    """Config file plus fakes for every network boundary."""
    config_path = write_config(tmp_path)
    candidates = [github_candidate()]
    calls = {"github": 0, "reddit": 0, "atlas": 0, "file": 0, "refresh": 0}

    def fake_github_search(config, store, token=None, **kwargs):
        calls["github"] += 1
        return list(candidates)

    def fake_reddit_scan(config, store):
        calls["reddit"] += 1
        return []

    def fake_load_atlas(config, token=None, **kwargs):
        calls["atlas"] += 1
        return AtlasSet(reasons={"known/repo": REASON_ATLAS})

    def fake_readme_sizes(config, store, cands, *, token, session=None):
        return {}

    def fake_tree_signals(config, store, cands, *, token, session=None, **kwargs):
        return {}

    def fake_score(config, candidate, *, readme_bytes=None,
                    tree_tests=None, tree_source_files=None, now=None):
        return cli.tiering_mod.TierScore(
            repo=candidate.repo,
            score=9.0,
            tier="a",
            label="scout:tier-a",
            components={"stars": cli.tiering_mod.Component(input=100, value=9.0)},
            absent=[],
            scored_at="2026-09-11T00:00:00Z",
        )

    def fake_file(config, store, cands, *, apply, token, session=None, **kwargs):
        calls["file"] += 1
        from scout import issues as issues_mod

        return [
            issues_mod.FiledIssue(
                repo=c.repo,
                status=issues_mod.STATUS_FILED if apply else issues_mod.STATUS_DRY_RUN,
                issue_number=7 if apply else None,
                title=issues_mod.render_title(c),
                body=issues_mod.render_body(c),
            )
            for c in cands
        ]

    def fake_refresh(config, store, *, token, session=None, apply=False, **kwargs):
        calls["refresh"] += 1
        return cli.refresh_mod.RefreshOutcome()

    monkeypatch.setattr(cli.github_search, "search", fake_github_search)
    monkeypatch.setattr(cli.reddit, "scan", fake_reddit_scan)
    monkeypatch.setattr(cli.atlas_mod, "load_atlas", fake_load_atlas)
    monkeypatch.setattr(cli.issues_mod, "file_candidates", fake_file)
    monkeypatch.setattr(cli.issues_mod, "migrate_existing_issues", lambda *args: 0)
    monkeypatch.setattr(
        cli.issues_mod, "fetch_existing_issue_repos", lambda *args, **kwargs: {}
    )
    monkeypatch.setattr(cli.tiering_mod, "collect_readme_sizes", fake_readme_sizes)
    monkeypatch.setattr(cli.tiering_mod, "collect_tree_signals", fake_tree_signals)
    monkeypatch.setattr(cli.tiering_mod, "score_candidate", fake_score)
    monkeypatch.setattr(cli.refresh_mod, "refresh", fake_refresh)
    monkeypatch.delenv("SCOUT_GITHUB_TOKEN", raising=False)
    return config_path, candidates, calls


def test_parser_requires_a_subcommand():
    with pytest.raises(SystemExit):
        main([])


def test_help_lists_subcommands(capsys):
    with pytest.raises(SystemExit) as exc:
        main(["--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out
    for name in ("scan", "check", "file", "run", "retract", "repair"):
        assert name in out


def test_scan_prints_candidates_one_per_line(wired, capsys):
    config_path, _, calls = wired
    assert main(["scan", "--config", str(config_path)]) == 0
    out = capsys.readouterr().out.strip()
    assert out == "new/hot\tgithub\thttps://github.com/new/hot\tterms: topic:agent-memory"
    assert calls["github"] == 1 and calls["reddit"] == 1


def test_scan_source_filter_skips_github(wired, capsys):
    config_path, _, calls = wired
    assert main(["scan", "--source", "reddit", "--config", str(config_path)]) == 0
    assert calls["github"] == 0 and calls["reddit"] == 1
    assert capsys.readouterr().out == ""


def test_scan_without_github_token_fails(wired, capsys, monkeypatch):
    config_path, _, _ = wired
    from scout.github_search import TokenMissingError

    def no_token(config, store, token=None, **kwargs):
        raise TokenMissingError("set SCOUT_GITHUB_TOKEN")

    monkeypatch.setattr(cli.github_search, "search", no_token)
    assert main(["scan", "--config", str(config_path)]) == 1
    assert "SCOUT_GITHUB_TOKEN" in capsys.readouterr().err


def test_check_marks_keep_and_skip(wired, capsys):
    config_path, candidates, _ = wired
    candidates.append(
        Candidate(repo="known/repo", source="reddit",
                  source_url="https://www.reddit.com/r/x/comments/1/y/")
    )
    assert main(["check", "--config", str(config_path)]) == 0
    out = capsys.readouterr().out
    assert "skip known/repo: already in the atlas" in out
    assert "keep new/hot (github)" in out


def test_file_dry_run_prints_exact_issue(wired, capsys):
    config_path, _, calls = wired
    assert main(["file", "--config", str(config_path)]) == 0
    out = capsys.readouterr().out
    assert "candidate: new/hot" in out
    assert "Repo: https://github.com/new/hot" in out
    assert calls["file"] == 1


def test_run_summary_and_log_entry(wired, capsys, tmp_path):
    config_path, _, _ = wired
    assert main(["run", "--config", str(config_path)]) == 0
    out = capsys.readouterr().out
    assert "seen: 1" in out
    assert "known: 0" in out
    assert "filed: 0" in out
    lines = (tmp_path / "state" / "run.log").read_text().splitlines()
    assert len(lines) == 1
    entry = json.loads(lines[0])
    assert entry["seen"] == 1
    assert entry["apply"] is False
    assert entry["filed"] == 0


def test_run_apply_files_and_counts_skips(wired, capsys, tmp_path, monkeypatch):
    config_path, candidates, _ = wired
    candidates.append(
        Candidate(repo="known/repo", source="reddit",
                  source_url="https://www.reddit.com/r/x/comments/1/y/")
    )
    monkeypatch.setenv("SCOUT_GITHUB_TOKEN", "t")
    assert main(["run", "--apply", "--config", str(config_path)]) == 0
    out = capsys.readouterr().out
    assert "filed new/hot -> issue #7" in out
    assert "skipped (already in the atlas): 1" in out
    entry = json.loads((tmp_path / "state" / "run.log").read_text().splitlines()[-1])
    assert entry["apply"] is True
    assert entry["filed"] == 1
    assert entry["known"] == 1
    assert entry["skipped"] == {"already in the atlas": 1}


def test_gather_runs_before_history_migration(wired, monkeypatch):
    config_path, _, _ = wired
    order: list[str] = []
    monkeypatch.setenv("SCOUT_GITHUB_TOKEN", "t")
    monkeypatch.setattr(
        cli.github_search, "search",
        lambda *args, **kwargs: order.append("gather") or [],
    )
    monkeypatch.setattr(
        cli.reddit, "scan", lambda *args, **kwargs: order.append("gather") or []
    )
    monkeypatch.setattr(
        cli.issues_mod, "migrate_existing_issues",
        lambda *args, **kwargs: order.append("migrate") or 0,
    )
    assert main(["run", "--config", str(config_path)]) == 0
    assert order.index("gather") < order.index("migrate")


def test_dotenv_fills_missing_token(tmp_path):
    env_path = tmp_path / ".env"
    env_path.write_text(
        '# local token\nSCOUT_GITHUB_TOKEN=file-token\nQUOTED=" spaced "\n'
    )
    sentinel = os.environ.get("SCOUT_GITHUB_TOKEN")
    os.environ.pop("SCOUT_GITHUB_TOKEN", None)
    try:
        cli._load_dotenv(env_path)
        assert os.environ["SCOUT_GITHUB_TOKEN"] == "file-token"
        assert os.environ["QUOTED"] == " spaced "
    finally:
        os.environ.pop("SCOUT_GITHUB_TOKEN", None)
        os.environ.pop("QUOTED", None)
        if sentinel is not None:
            os.environ["SCOUT_GITHUB_TOKEN"] = sentinel


def test_dotenv_does_not_override_existing(tmp_path, monkeypatch):
    env_path = tmp_path / ".env"
    env_path.write_text("SCOUT_GITHUB_TOKEN=file-token\n")
    monkeypatch.setenv("SCOUT_GITHUB_TOKEN", "env-token")
    cli._load_dotenv(env_path)
    assert os.environ["SCOUT_GITHUB_TOKEN"] == "env-token"


def test_run_calls_refresh_after_filing_and_reports_its_outcome(
    wired, capsys, tmp_path, monkeypatch
):
    config_path, _, calls = wired
    order: list[str] = []

    def ordering_file(config, store, cands, *, apply, token, session=None, **kwargs):
        order.append("file")
        return []

    def ordering_refresh(config, store, *, token, session=None, apply=False, **kwargs):
        order.append("refresh")
        calls["refresh"] += 1
        return cli.refresh_mod.RefreshOutcome(
            refreshed=["owner/refreshed"],
            tier_changes=[("owner/refreshed", "b", "a")],
            failed=["owner/dead"],
        )

    monkeypatch.setattr(cli.issues_mod, "file_candidates", ordering_file)
    monkeypatch.setattr(cli.refresh_mod, "refresh", ordering_refresh)

    assert main(["run", "--config", str(config_path)]) == 0
    assert order == ["file", "refresh"]
    assert calls["refresh"] == 1

    reports = list((tmp_path / "reports").glob("*.md"))
    assert len(reports) == 1
    text = reports[0].read_text()
    assert "## Refresh" in text
    assert "- refreshed: 1" in text
    assert "owner/refreshed: B -> A" in text
    assert "- failed: 1" in text
    assert "owner/dead" in text


def test_run_writes_markdown_report_in_dry_mode(wired, capsys, tmp_path):
    config_path, _, _ = wired
    assert main(["run", "--config", str(config_path)]) == 0
    reports = list((tmp_path / "reports").glob("*.md"))
    assert len(reports) == 1
    name = reports[0].name
    assert len(name) == len("2026-09-11T05-30-00Z.md")
    assert name[4] == "-" and name[10] == "T" and name.endswith("Z.md")
    text = reports[0].read_text()
    assert "\u2014" not in text
    assert "## Candidates seen: 1" in text
    assert "topic:agent-memory: 1" in text
    assert "new/hot - 9.00 (scout:tier-a)" in text
    assert "- A: 1" in text
    assert "report:" in capsys.readouterr().out


def test_check_then_file_walks_target_issues_once_and_shares(tmp_path, monkeypatch):
    from scout.config import load as load_config
    from scout.store import Store
    from scout.tiering import TierScore

    config = load_config(write_config(tmp_path))
    store = Store(config.state.db_path, config.state.candidates_path)
    candidates = [github_candidate()]
    store.record_candidates(candidates)

    class FakeResponse:
        status_code = 200

        def json(self):
            return [{"title": "candidate: other/repo", "number": 9,
                     "labels": [{"name": "scout:candidate"}]}]

        def raise_for_status(self):
            return None

    class CountingSession:
        def __init__(self):
            self.issue_walks = 0

        def get(self, url, params=None, headers=None, timeout=None):
            assert url.endswith("/issues")
            self.issue_walks += 1
            return FakeResponse()

    shared = {}

    def fake_load_atlas(config, token=None, session=None, **kwargs):
        shared["atlas"] = kwargs.get("filed_repos")
        return AtlasSet()

    def fake_file(config, store, cands, *, apply, token, session=None, **kwargs):
        shared["file"] = kwargs.get("existing")
        return []

    monkeypatch.setattr(cli.atlas_mod, "load_atlas", fake_load_atlas)
    monkeypatch.setattr(cli.issues_mod, "file_candidates", fake_file)
    monkeypatch.setattr(cli.tiering_mod, "collect_readme_sizes",
                        lambda *args, **kwargs: {})
    monkeypatch.setattr(cli.tiering_mod, "collect_tree_signals",
                        lambda *args, **kwargs: {})
    monkeypatch.setattr(
        cli.tiering_mod, "score_candidate",
        lambda config, candidate, **kwargs: TierScore(
            repo=candidate.repo, score=9.0, tier="a", label="scout:tier-a",
            components={},
        ),
    )

    session = CountingSession()
    outcome = cli._check_then_file(
        config, store, candidates, apply=False, token="t", session=session
    )
    assert session.issue_walks == 1
    assert shared["atlas"] == {"other/repo": 9}
    assert shared["file"] == {"other/repo": 9}
    assert [c.repo for c in outcome.kept] == ["new/hot"]
    store.close()


def test_check_then_file_passes_tree_signals_into_score_candidate(
    tmp_path, monkeypatch
):
    """A repo collect_tree_signals has data for must reach score_candidate
    as (tree_tests, tree_source_files); a repo missing from that map must
    reach it as (None, None), not silently dropped or mixed up."""
    from scout.config import load as load_config
    from scout.store import Store
    from scout.tiering import TierScore

    config = load_config(write_config(tmp_path))
    store = Store(config.state.db_path, config.state.candidates_path)
    candidates = [github_candidate(), github_candidate(repo="owner/notree")]
    store.record_candidates(candidates)

    monkeypatch.setattr(cli.atlas_mod, "load_atlas", lambda *a, **k: AtlasSet())
    monkeypatch.setattr(cli.issues_mod, "file_candidates", lambda *a, **k: [])
    monkeypatch.setattr(
        cli.issues_mod, "fetch_existing_issue_repos", lambda *a, **k: {}
    )
    monkeypatch.setattr(cli.tiering_mod, "collect_readme_sizes", lambda *a, **k: {})
    monkeypatch.setattr(
        cli.tiering_mod, "collect_tree_signals",
        lambda *a, **k: {"new/hot": (True, 12)},
    )

    received: dict[str, tuple] = {}

    def recording_score(config, candidate, *, readme_bytes=None,
                          tree_tests=None, tree_source_files=None, now=None):
        received[candidate.repo] = (tree_tests, tree_source_files)
        return TierScore(
            repo=candidate.repo, score=1.0, tier="c", label="scout:tier-c",
            components={},
        )

    monkeypatch.setattr(cli.tiering_mod, "score_candidate", recording_score)

    cli._check_then_file(config, store, candidates, apply=False, token="t")

    assert received["new/hot"] == (True, 12)
    assert received["owner/notree"] == (None, None)
    store.close()


def test_run_dry_mode_persists_scores_and_known_repos(wired, tmp_path):
    config_path, candidates, _ = wired
    candidates.append(
        Candidate(repo="known/repo", source="reddit",
                  source_url="https://www.reddit.com/r/x/comments/1/y/")
    )
    assert main(["run", "--config", str(config_path)]) == 0

    history_path = tmp_path / "data" / "candidates.jsonl"
    rows = {
        json.loads(line)["repo"]: json.loads(line)
        for line in history_path.read_text().splitlines()
        if json.loads(line).get("kind") == "repo"
    }
    assert rows["new/hot"]["score"] == 9.0
    assert rows["new/hot"]["tier"] == "a"
    assert rows["new/hot"]["assessment"] == "tier-a"
    assert rows["known/repo"]["assessment"] == "atlas-known"
    assert "score" not in rows["known/repo"]


def test_run_marks_atlas_known_only_for_atlas_and_archive_reasons(
    wired, tmp_path, monkeypatch
):
    """A scout issue already filed says nothing about the atlas: the row's
    earlier assessment must survive, while atlas and archive hits flip
    to atlas-known."""
    config_path, candidates, _ = wired
    candidates.extend([
        Candidate(repo="filed/repo", source="reddit",
                  source_url="https://www.reddit.com/r/x/comments/1/a/"),
        Candidate(repo="archived/repo", source="reddit",
                  source_url="https://www.reddit.com/r/x/comments/1/b/"),
    ])
    monkeypatch.setattr(
        cli.atlas_mod, "load_atlas",
        lambda config, token=None, **kwargs: AtlasSet(reasons={
            "known/repo": REASON_ATLAS,
            "archived/repo": REASON_ARCHIVE,
            "filed/repo": REASON_ISSUE,
        }),
    )
    history_path = tmp_path / "data" / "candidates.jsonl"
    from scout.store import Store

    with Store(tmp_path / "state" / "scout.db", history_path) as store:
        store.record_candidates(candidates)
        store.history.repos["filed/repo"]["assessment"] = "tier-b"

    assert main(["run", "--config", str(config_path)]) == 0

    rows = {
        json.loads(line)["repo"]: json.loads(line)
        for line in history_path.read_text().splitlines()
        if json.loads(line).get("kind") == "repo"
    }
    assert rows["archived/repo"]["assessment"] == "atlas-known"
    assert rows["filed/repo"]["assessment"] == "tier-b"
    assert "score" not in rows["filed/repo"]


def test_repair_flags_title_only_and_prints_counts(wired, capsys):
    config_path, _, _ = wired
    assert main(["repair", "--config", str(config_path)]) == 0
    out = capsys.readouterr().out
    assert "flagged 0 title-only history rows" in out
    assert "repaired 0 rows: discovery" in out
    assert "repaired 0 rows: assessment" in out
    assert "repaired 0 rows: readme_bytes" in out


def test_repair_accepts_retracted_through(tmp_path, monkeypatch):
    config_path = write_config(tmp_path)
    history_path = tmp_path / "data" / "candidates.jsonl"
    from scout.store import Store

    with Store(tmp_path / "state" / "scout.db", history_path) as store:
        # a row with a candidate payload, so mark_title_only_rows leaves it alone
        store.record_candidates([github_candidate("owner/old")])
        store.history.mark_issue("owner/old", 5)
        del store.history.repos["owner/old"]["discovery"]

    monkeypatch.delenv("SCOUT_GITHUB_TOKEN", raising=False)
    assert main([
        "repair", "--config", str(config_path), "--retracted-through", "100",
    ]) == 0

    restored = Store(tmp_path / "state" / "scout.db", history_path)
    assert restored.history.repos["owner/old"]["discovery"] == "retracted"
    restored.close()
