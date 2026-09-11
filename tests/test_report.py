from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from scout.models import Candidate
from scout.report import RunReport, render_report, write_report
from scout.tiering import TierScore

RAN_AT = datetime(2026, 9, 11, 5, 30, tzinfo=UTC)


def candidate(repo: str, description: str = "memory store for agents") -> Candidate:
    return Candidate(
        repo=repo,
        source="github",
        source_url=f"https://github.com/{repo}",
        matched_terms=["topic:agent-memory"],
        description=description,
        stars=10,
        html_url=f"https://github.com/{repo}",
    )


def score(repo: str, value: float, tier: str) -> TierScore:
    return TierScore(
        repo=repo, score=value, tier=tier, label=f"scout:tier-{tier}",
        components={"stars": value},
    )


def sample_report() -> RunReport:
    kept = [
        candidate("alice/memorymesh"),
        candidate("bob/context-store", "agent memory"),
        candidate("carol/tinymem", "barely any signal here"),
        candidate("dave/oldmem", "an old agent memory project"),
    ]
    return RunReport(
        queries={"topic:agent-memory": 2, "topic:ai-memory": 1},
        seen=7,
        known={
            "already in the atlas": ["known/repo"],
            "scout issue already filed": ["filed/repo"],
        },
        dropped={
            "term gate": ["drop/one", "drop/two"],
            "profile repo": ["alice/alice"],
            "tier C": ["carol/tinymem"],
        },
        scores={
            "alice/memorymesh": score("alice/memorymesh", 9.5, "a"),
            "bob/context-store": score("bob/context-store", 6.0, "b"),
            "carol/tinymem": score("carol/tinymem", 2.0, "c"),
            "dave/oldmem": score("dave/oldmem", 8.1, "a"),
        },
        candidates={c.repo: c for c in kept},
    )


def test_render_contains_every_section():
    text = render_report(sample_report(), RAN_AT)
    assert "# Scout run report - 2026-09-11T05:30:00Z" in text
    assert "topic:agent-memory: 2" in text
    assert "## Candidates seen: 7" in text
    assert "already in the atlas: 1" in text
    assert "- known/repo" in text
    assert "term gate: 2" in text
    assert "tier C: 1" in text
    assert "- A: 2" in text
    assert "- B: 1" in text
    assert "- C: 1" in text
    assert "## Top 20 by score" in text


def test_top_list_is_ranked_and_described():
    text = render_report(sample_report(), RAN_AT)
    lines = [line for line in text.splitlines() if line[:2] in {"1.", "2.", "3.", "4."}]
    assert lines[0].startswith("1. alice/memorymesh - 9.50 (scout:tier-a)")
    assert lines[1].startswith("2. dave/oldmem - 8.10")
    assert lines[3].startswith("4. carol/tinymem - 2.00")
    assert "memory store for agents" in lines[0]
    assert "barely any signal here" in lines[3]


def test_no_em_dash_anywhere():
    text = render_report(sample_report(), RAN_AT)
    assert "\u2014" not in text


def test_empty_report_renders_without_error():
    text = render_report(RunReport(), RAN_AT)
    assert "## Candidates seen: 0" in text
    assert "\u2014" not in text


def test_write_report_names_file_by_utc_timestamp(tmp_path):
    path = write_report(sample_report(), tmp_path / "reports", RAN_AT)
    assert path == Path(tmp_path / "reports" / "2026-09-11T05-30-00Z.md")
    assert path.exists()
    assert "\u2014" not in path.read_text()


def test_long_descriptions_are_truncated_to_one_line():
    long = candidate("eve/chatty", "word " * 60)
    report = RunReport(
        seen=1,
        scores={"eve/chatty": score("eve/chatty", 1.0, "c")},
        candidates={"eve/chatty": long},
    )
    text = render_report(report, RAN_AT)
    line = next(l for l in text.splitlines() if l.startswith("1. "))
    assert len(line) < 160
    assert "..." in line
