"""scout command line interface.

Every subcommand is a dry run unless --apply is passed, and the only
command that ever writes to GitHub is what --apply gates. Discovery state is
persisted separately from the disposable local SQLite cache, so check, file
and run can reuse pending candidates without rediscovering them.
"""

from __future__ import annotations

import argparse
import json
import os
import requests
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from . import atlas as atlas_mod
from . import github_search, issues as issues_mod, reddit, retract as retract_mod
from . import report as report_mod
from . import tiering as tiering_mod
from .config import Config, load
from .models import Candidate
from .store import Store

DEFAULT_CONFIG = Path(__file__).resolve().parent.parent.parent / "scout.toml"
TOKEN_ENV = "SCOUT_GITHUB_TOKEN"


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG,
        help="path to scout.toml (default: the one next to this repo)",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="scout",
        description="Candidate finder for the Agent Memory Atlas.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_scan = sub.add_parser("scan", help="collect candidates from the sources")
    _add_common(p_scan)
    p_scan.add_argument(
        "--source", choices=["github", "reddit", "all"], default="all"
    )

    p_check = sub.add_parser("check", help="drop candidates the atlas already holds")
    _add_common(p_check)

    p_file = sub.add_parser("file", help="print or file one issue per candidate")
    _add_common(p_file)
    p_file.add_argument("--apply", action="store_true", help="create the issues")

    p_run = sub.add_parser("run", help="scan, check and file in one pass")
    _add_common(p_run)
    p_run.add_argument("--apply", action="store_true", help="create the issues")

    p_retract = sub.add_parser(
        "retract",
        help="close a misfired candidate batch under scout:retracted",
    )
    _add_common(p_retract)
    p_retract.add_argument(
        "--apply", action="store_true", help="close the issues and pin the explainer"
    )

    p_repair = sub.add_parser(
        "repair", help="flag history rows that carry no candidate payload"
    )
    _add_common(p_repair)
    p_repair.add_argument(
        "--retracted-through",
        type=int,
        default=None,
        help="issue numbers at or below this were closed in a retraction",
    )

    return parser


def _token() -> str | None:
    return os.environ.get(TOKEN_ENV) or None


def _load_dotenv(path: Path) -> None:
    """Fill missing environment variables from a .env file, if present."""
    if not path.is_file():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def _gather(
    config: Config,
    store: Store,
    source: str,
    token: str | None,
    *,
    include_pending: bool = False,
    drops: dict[str, list[str]] | None = None,
) -> list[Candidate]:
    candidates: list[Candidate] = []
    if source in ("github", "all"):
        candidates.extend(
            github_search.search(config, store, token=token, drops=drops)
        )
    if source in ("reddit", "all"):
        candidates.extend(reddit.scan(config, store))
    store.record_candidates(candidates)
    if not include_pending:
        return candidates
    known_repos = {candidate.repo for candidate in candidates}
    pending = [
        candidate
        for candidate in store.pending_candidates()
        if candidate.repo not in known_repos
    ]
    return [*candidates, *pending]


@dataclass
class CheckFileOutcome:
    """Result of the atlas check, tiering and filing in one pass."""

    kept: list[Candidate]
    known: dict[str, list[str]]  # skip reason -> repos
    scores: dict[str, tiering_mod.TierScore]
    results: list[issues_mod.FiledIssue]


def _check_then_file(
    config: Config,
    store: Store,
    candidates: list[Candidate],
    *,
    apply: bool,
    token: str | None,
    session: requests.Session | None = None,
) -> CheckFileOutcome:
    """Atlas check, tiering, then issue filing. The target-repo issue walk
    runs once here and is shared by the atlas check and filing."""
    if session is None:
        session = requests.Session()
    existing = issues_mod.fetch_existing_issue_repos(
        session, config, issues_mod._headers(token)
    )
    known = atlas_mod.load_atlas(
        config, token=token, session=session, filed_repos=existing
    )
    kept: list[Candidate] = []
    known_reasons: dict[str, list[str]] = {}
    for candidate in candidates:
        reason = known.is_known(candidate.repo)
        if reason:
            known_reasons.setdefault(reason, []).append(candidate.repo)
        else:
            kept.append(candidate)

    readme_sizes = tiering_mod.collect_readme_sizes(
        config, store, kept, token=token, session=session
    )
    tree_signals = tiering_mod.collect_tree_signals(
        config, store, kept, token=token, session=session
    )
    scores = {}
    for candidate in kept:
        signals = tree_signals.get(candidate.repo)
        tree_tests, tree_source_files = signals if signals else (None, None)
        scores[candidate.repo] = tiering_mod.score_candidate(
            config, candidate,
            readme_bytes=readme_sizes.get(candidate.repo),
            tree_tests=tree_tests,
            tree_source_files=tree_source_files,
        )
    for repo, score in scores.items():
        store.record_score(repo, score)
    for repos in known_reasons.values():
        for repo in repos:
            store.mark_known(repo)
    results = issues_mod.file_candidates(
        config, store, kept, apply=apply, token=token, session=session,
        tiers=scores, existing=existing,
    )
    return CheckFileOutcome(
        kept=kept, known=known_reasons, scores=scores, results=results
    )


def _migrate_history(config: Config, store: Store, token: str | None) -> bool:
    if not token or store.issues_migrated:
        return True
    try:
        imported = issues_mod.migrate_existing_issues(config, store, token)
    except requests.RequestException as exc:
        print(f"history migration failed: {exc}", file=sys.stderr)
        return False
    print(f"history: imported {imported} existing candidate issues", file=sys.stderr)
    return True


def _print_file_results(results: list[issues_mod.FiledIssue]) -> None:
    for result in results:
        if result.status == issues_mod.STATUS_DRY_RUN:
            print(result.title)
            print()
            print(result.body)
            print()
        elif result.status == issues_mod.STATUS_FILED:
            print(f"filed {result.repo} -> issue #{result.issue_number}")
        else:
            print(f"skipped {result.repo}: {result.reason}")


def _cmd_scan(args: argparse.Namespace, config: Config, store: Store) -> int:
    try:
        candidates = _gather(config, store, args.source, _token())
    except github_search.TokenMissingError as exc:
        print(f"scan: {exc}", file=sys.stderr)
        return 1
    for candidate in candidates:
        terms = ", ".join(candidate.matched_terms)
        print(f"{candidate.repo}\t{candidate.source}\t{candidate.source_url}"
              f"\tterms: {terms}")
    return 0


def _cmd_check(args: argparse.Namespace, config: Config, store: Store) -> int:
    try:
        candidates = _gather(
            config, store, "all", _token(), include_pending=True
        )
    except github_search.TokenMissingError as exc:
        print(f"check: {exc}", file=sys.stderr)
        return 1
    if not _migrate_history(config, store, _token()):
        return 1
    known = atlas_mod.load_atlas(config, token=_token())
    for candidate in candidates:
        reason = known.is_known(candidate.repo)
        if reason:
            print(f"skip {candidate.repo}: {reason}")
        else:
            print(f"keep {candidate.repo} ({candidate.source})")
    return 0


def _cmd_file(args: argparse.Namespace, config: Config, store: Store) -> int:
    token = _token()
    try:
        candidates = _gather(
            config, store, "all", token, include_pending=True
        )
    except github_search.TokenMissingError as exc:
        print(f"file: {exc}", file=sys.stderr)
        return 1
    if not _migrate_history(config, store, token):
        return 1
    try:
        outcome = _check_then_file(
            config, store, candidates, apply=args.apply, token=token
        )
    except issues_mod.TokenMissingError as exc:
        print(f"file: {exc}", file=sys.stderr)
        return 1
    for result in outcome.results:
        if result.issue_number is not None:
            store.mark_history_issue(result.repo, result.issue_number)
    _print_file_results(outcome.results)
    return 0


def _cmd_run(args: argparse.Namespace, config: Config, store: Store) -> int:
    token = _token()
    drops: dict[str, list[str]] = {}
    try:
        candidates = _gather(
            config, store, "all", token, include_pending=True, drops=drops
        )
    except github_search.TokenMissingError as exc:
        print(f"run: {exc}", file=sys.stderr)
        return 1
    if not _migrate_history(config, store, token):
        return 1
    try:
        outcome = _check_then_file(
            config, store, candidates, apply=args.apply, token=token
        )
    except issues_mod.TokenMissingError as exc:
        print(f"run: {exc}", file=sys.stderr)
        return 1

    skipped = {reason: len(repos) for reason, repos in outcome.known.items()}
    filed = 0
    for result in outcome.results:
        if result.status == issues_mod.STATUS_FILED:
            filed += 1
            if result.issue_number is not None:
                store.mark_history_issue(result.repo, result.issue_number)
            print(f"filed {result.repo} -> issue #{result.issue_number}")
        elif result.status == issues_mod.STATUS_SKIPPED:
            if result.issue_number is not None:
                store.mark_history_issue(result.repo, result.issue_number)
            reason = result.reason or "skipped"
            skipped[reason] = skipped.get(reason, 0) + 1

    entry = {
        "ran_at": datetime.now(UTC).isoformat(),
        "apply": bool(args.apply),
        "seen": len(candidates),
        "known": sum(len(repos) for repos in outcome.known.values()),
        "filed": filed,
        "skipped": skipped,
    }
    log_path = config.state.db_path.parent / "run.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a") as fh:
        fh.write(json.dumps(entry) + "\n")

    report = report_mod.RunReport(
        queries=_query_counts(candidates),
        seen=len(candidates),
        known=outcome.known,
        dropped=_drop_counts(drops, outcome),
        scores=outcome.scores,
        candidates={candidate.repo: candidate for candidate in outcome.kept},
    )
    report_path = report_mod.write_report(report, config.state.reports_path)

    print(f"seen: {entry['seen']}")
    print(f"known: {entry['known']}")
    print(f"filed: {filed}")
    for reason, count in sorted(skipped.items()):
        print(f"skipped ({reason}): {count}")
    print(f"run log: {log_path}")
    print(f"report: {report_path}")
    return 0


def _query_counts(candidates: list[Candidate]) -> dict[str, int]:
    """Candidates per GitHub search query (reddit candidates carry terms,
    not queries, so they land outside this count)."""
    counts: dict[str, int] = {}
    for candidate in candidates:
        if candidate.source != "github" or not candidate.matched_terms:
            continue
        query = candidate.matched_terms[0]
        counts[query] = counts.get(query, 0) + 1
    return counts


def _drop_counts(
    drops: dict[str, list[str]], outcome: CheckFileOutcome
) -> dict[str, list[str]]:
    """Drop reasons from the scan plus the tier-C repos held back this run."""
    counts = {reason: list(repos) for reason, repos in drops.items()}
    tier_c = sorted(
        score.repo for score in outcome.scores.values() if score.tier == "c"
    )
    if tier_c:
        counts[tiering_mod.REASON_TIER_C] = tier_c
    return counts


def _cmd_retract(args: argparse.Namespace, config: Config, store: Store) -> int:
    token = _token()
    try:
        result = retract_mod.retract(
            config, apply=args.apply, token=token, store=store
        )
    except retract_mod.TokenMissingError as exc:
        print(f"retract: {exc}", file=sys.stderr)
        return 1
    except requests.RequestException as exc:
        print(f"retract: {exc}", file=sys.stderr)
        return 1
    verb = "closed" if result.apply else "would close"
    for number, title, _labels in result.issues:
        print(f"{verb} #{number}: {title}")
    print(f"{verb}: {result.found}")
    if result.apply and result.explainer_number is not None:
        pin_state = "pinned" if result.pinned else "not pinned, see warning above"
        print(f"explainer: issue #{result.explainer_number} ({pin_state})")
    elif not result.apply:
        print("dry run; pass --apply to close and pin the explainer")
    return 0


def _cmd_repair(args: argparse.Namespace, config: Config, store: Store) -> int:
    marked = store.mark_title_only_rows()
    print(f"flagged {marked} title-only history rows")
    counts = store.repair_rows(retracted_through=args.retracted_through)
    for key in ("discovery", "assessment", "readme_bytes"):
        print(f"repaired {counts.get(key, 0)} rows: {key}")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    _load_dotenv(args.config.parent / ".env")
    config = load(args.config)  # fail early on a broken config file

    with Store(config.state.db_path, config.state.candidates_path) as store:
        if args.command == "scan":
            return _cmd_scan(args, config, store)
        if args.command == "check":
            return _cmd_check(args, config, store)
        if args.command == "file":
            return _cmd_file(args, config, store)
        if args.command == "run":
            return _cmd_run(args, config, store)
        if args.command == "retract":
            return _cmd_retract(args, config, store)
        if args.command == "repair":
            return _cmd_repair(args, config, store)
    return 2


if __name__ == "__main__":
    sys.exit(main())
