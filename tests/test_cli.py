from __future__ import annotations

import json
import os

import pytest

from scout import cli
from scout.atlas import AtlasSet, REASON_ATLAS
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

[state]
db_path = "{db_path}"
candidates_path = "{history_path}"
"""


def write_config(tmp_path) -> object:
    path = tmp_path / "scout.toml"
    path.write_text(CONFIG_TEMPLATE.format(
        db_path=tmp_path / "state" / "scout.db",
        history_path=tmp_path / "data" / "candidates.jsonl",
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
    calls = {"github": 0, "reddit": 0, "atlas": 0, "file": 0}

    def fake_github_search(config, store, token=None):
        calls["github"] += 1
        return list(candidates)

    def fake_reddit_scan(config, store):
        calls["reddit"] += 1
        return []

    def fake_load_atlas(config, token=None):
        calls["atlas"] += 1
        return AtlasSet(reasons={"known/repo": REASON_ATLAS})

    def fake_file(config, store, cands, *, apply, token, session=None):
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

    monkeypatch.setattr(cli.github_search, "search", fake_github_search)
    monkeypatch.setattr(cli.reddit, "scan", fake_reddit_scan)
    monkeypatch.setattr(cli.atlas_mod, "load_atlas", fake_load_atlas)
    monkeypatch.setattr(cli.issues_mod, "file_candidates", fake_file)
    monkeypatch.setattr(cli.issues_mod, "migrate_existing_issues", lambda *args: 0)
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
    for name in ("scan", "check", "file", "run"):
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

    def no_token(config, store, token=None):
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
