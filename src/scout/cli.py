"""scout command line interface.

Every subcommand is a dry run unless --apply is passed, and the only
command that ever writes to GitHub is what --apply gates. Each of check,
file and run re-scans the sources on purpose: the volumes are small and
the store keeps reruns idempotent.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

from . import atlas as atlas_mod
from . import github_search, issues as issues_mod, reddit
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

    return parser


def _token() -> str | None:
    return os.environ.get(TOKEN_ENV) or None


def _gather(
    config: Config, store: Store, source: str, token: str | None
) -> list[Candidate]:
    candidates: list[Candidate] = []
    if source in ("github", "all"):
        candidates.extend(github_search.search(config, store, token=token))
    if source in ("reddit", "all"):
        candidates.extend(reddit.scan(config, store))
    return candidates


def _check_then_file(
    config: Config,
    store: Store,
    candidates: list[Candidate],
    *,
    apply: bool,
    token: str | None,
) -> tuple[list[Candidate], dict[str, int], list[issues_mod.FiledIssue]]:
    """Atlas check then issue filing. Returns kept candidates, atlas skip
    counts per reason, and the per-candidate filing results."""
    known = atlas_mod.load_atlas(config, token=token)
    kept: list[Candidate] = []
    skipped: dict[str, int] = {}
    for candidate in candidates:
        reason = known.is_known(candidate.repo)
        if reason:
            skipped[reason] = skipped.get(reason, 0) + 1
        else:
            kept.append(candidate)
    results = issues_mod.file_candidates(
        config, store, kept, apply=apply, token=token
    )
    return kept, skipped, results


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
        candidates = _gather(config, store, "all", _token())
    except github_search.TokenMissingError as exc:
        print(f"check: {exc}", file=sys.stderr)
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
        candidates = _gather(config, store, "all", token)
    except github_search.TokenMissingError as exc:
        print(f"file: {exc}", file=sys.stderr)
        return 1
    try:
        _, _, results = _check_then_file(
            config, store, candidates, apply=args.apply, token=token
        )
    except issues_mod.TokenMissingError as exc:
        print(f"file: {exc}", file=sys.stderr)
        return 1
    _print_file_results(results)
    return 0


def _cmd_run(args: argparse.Namespace, config: Config, store: Store) -> int:
    token = _token()
    try:
        candidates = _gather(config, store, "all", token)
    except github_search.TokenMissingError as exc:
        print(f"run: {exc}", file=sys.stderr)
        return 1
    try:
        _, known_skipped, results = _check_then_file(
            config, store, candidates, apply=args.apply, token=token
        )
    except issues_mod.TokenMissingError as exc:
        print(f"run: {exc}", file=sys.stderr)
        return 1

    skipped = dict(known_skipped)
    filed = 0
    for result in results:
        if result.status == issues_mod.STATUS_FILED:
            filed += 1
            print(f"filed {result.repo} -> issue #{result.issue_number}")
        elif result.status == issues_mod.STATUS_SKIPPED:
            reason = result.reason or "skipped"
            skipped[reason] = skipped.get(reason, 0) + 1

    entry = {
        "ran_at": datetime.now(UTC).isoformat(),
        "apply": bool(args.apply),
        "seen": len(candidates),
        "known": sum(known_skipped.values()),
        "filed": filed,
        "skipped": skipped,
    }
    log_path = config.state.db_path.parent / "run.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a") as fh:
        fh.write(json.dumps(entry) + "\n")

    print(f"seen: {entry['seen']}")
    print(f"known: {entry['known']}")
    print(f"filed: {filed}")
    for reason, count in sorted(skipped.items()):
        print(f"skipped ({reason}): {count}")
    print(f"run log: {log_path}")
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    config = load(args.config)  # fail early on a broken config file

    with Store(config.state.db_path) as store:
        if args.command == "scan":
            return _cmd_scan(args, config, store)
        if args.command == "check":
            return _cmd_check(args, config, store)
        if args.command == "file":
            return _cmd_file(args, config, store)
        if args.command == "run":
            return _cmd_run(args, config, store)
    return 2


if __name__ == "__main__":
    sys.exit(main())
