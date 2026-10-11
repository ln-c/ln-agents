"""Command line: `python -m agentic_pipeline validate [--root PATH]`."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from agentic_pipeline.graph import REPO_ROOT, validate_repo


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agentic_pipeline")
    sub = parser.add_subparsers(dest="command", required=True)
    val = sub.add_parser("validate", help="check pipeline graph and policy invariants")
    val.add_argument("--root", type=Path, default=REPO_ROOT)
    args = parser.parse_args(argv)

    errors = validate_repo(args.root)
    for err in errors:
        print(f"ERROR: {err}", file=sys.stderr)
    if errors:
        return 1
    print("pipeline graph OK")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
