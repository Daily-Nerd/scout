"""Per-run markdown report.

One plain-text markdown file per run, named by UTC timestamp, covering
what the run saw: per-query candidate counts, candidates seen, known
repos and why, dropped repos and why, the tier histogram, and the top
candidates by score. No attribution and no em-dashes anywhere.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from .models import Candidate
from .tiering import TIER_A, TIER_B, TIER_C, TierScore

TOP_N = 20
_DESCRIPTION_LIMIT = 100


@dataclass
class RunReport:
    """Everything a run report needs, collected by the pipeline."""

    queries: dict[str, int] = field(default_factory=dict)
    seen: int = 0
    known: dict[str, list[str]] = field(default_factory=dict)
    dropped: dict[str, list[str]] = field(default_factory=dict)
    scores: dict[str, TierScore] = field(default_factory=dict)
    candidates: dict[str, Candidate] = field(default_factory=dict)


def _one_line(description: str) -> str:
    line = " ".join(description.split())
    if len(line) > _DESCRIPTION_LIMIT:
        line = line[: _DESCRIPTION_LIMIT - 3].rstrip() + "..."
    return line or "no description"


def render_report(report: RunReport, ran_at: datetime | None = None) -> str:
    """Render the report as plain-text markdown. Pure: no IO."""
    ran_at = ran_at or datetime.now(UTC)
    lines = [
        f"# Scout run report - {ran_at.strftime('%Y-%m-%dT%H:%M:%SZ')}",
        "",
        "## Candidates per query",
        "",
    ]
    if report.queries:
        for query, count in sorted(report.queries.items()):
            lines.append(f"- {query}: {count}")
    else:
        lines.append("- none")
    lines.extend(["", f"## Candidates seen: {report.seen}", "", "## Known (skipped)", ""])
    if report.known:
        for reason, repos in sorted(report.known.items()):
            lines.append(f"- {reason}: {len(repos)}")
            for repo in sorted(repos):
                lines.append(f"  - {repo}")
    else:
        lines.append("- none")

    lines.extend(["", "## Dropped", ""])
    if report.dropped:
        for reason, repos in sorted(report.dropped.items()):
            lines.append(f"- {reason}: {len(repos)}")
            for repo in sorted(repos):
                lines.append(f"  - {repo}")
    else:
        lines.append("- none")

    histogram = {TIER_A: 0, TIER_B: 0, TIER_C: 0}
    for score in report.scores.values():
        histogram[score.tier] = histogram.get(score.tier, 0) + 1
    lines.extend(["", "## Tier histogram", ""])
    for tier in (TIER_A, TIER_B, TIER_C):
        lines.append(f"- {tier.upper()}: {histogram.get(tier, 0)}")
    partial_count = sum(1 for score in report.scores.values() if score.absent)
    lines.append(f"- scored with absent components: {partial_count}")

    ranked = sorted(report.scores.values(), key=lambda s: (-s.score, s.repo))
    lines.extend(["", f"## Top {TOP_N} by score", ""])
    if ranked:
        for rank, score in enumerate(ranked[:TOP_N], start=1):
            candidate = report.candidates.get(score.repo)
            description = _one_line(candidate.description if candidate else "")
            line = (
                f"{rank}. {score.repo} - {score.score:.2f} "
                f"({score.label}) - {description}"
            )
            if score.absent:
                line += " partial"
            lines.append(line)
    else:
        lines.append("- none")
    lines.append("")
    return "\n".join(lines)


def write_report(
    report: RunReport,
    directory: str | Path,
    ran_at: datetime | None = None,
) -> Path:
    """Write the report to <directory>/<UTC timestamp>.md and return the path."""
    ran_at = ran_at or datetime.now(UTC)
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{ran_at.strftime('%Y-%m-%dT%H-%M-%SZ')}.md"
    path.write_text(render_report(report, ran_at))
    return path
