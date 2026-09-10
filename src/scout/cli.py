"""scout command line interface.

Every subcommand is a dry run unless --apply is passed, and the only
command that ever writes to GitHub is what --apply gates.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .config import load

DEFAULT_CONFIG = Path(__file__).resolve().parent.parent.parent / "scout.toml"


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


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    load(args.config)  # fail early on a broken config file

    if args.command == "scan":
        print("scan: sources are not wired up yet")
        return 1
    if args.command == "check":
        print("check: atlas lookup is not wired up yet")
        return 1
    if args.command == "file":
        print("file: issue filing is not wired up yet")
        return 1
    if args.command == "run":
        print("run: pipeline is not wired up yet")
        return 1
    return 2


if __name__ == "__main__":
    sys.exit(main())
