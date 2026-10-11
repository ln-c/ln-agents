"""Command line: `python -m agentic_pipeline {validate,preflight} [--root PATH]`."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from agentic_pipeline.graph import REPO_ROOT
from agentic_pipeline.readiness import Check, Report, evaluate_stage, run_preflight
from agentic_pipeline.validation import validate_repo


def _validate(root: Path) -> int:
    errors = validate_repo(root)
    for err in errors:
        print(f"ERROR: {err}", file=sys.stderr)
    if errors:
        return 1
    print("repository validation OK (graph, policies, model registry)")
    return 0


def _print_checks(title: str, checks: list[Check]) -> None:
    print(f"{title}:")
    for c in checks:
        print(f"  {c.status:<10}  {c.name:<26}  {c.detail}")


def _print_report(report: Report) -> None:
    _print_checks("global", report.global_checks)
    for name, checks in report.providers.items():
        _print_checks(name, checks)
    _print_checks("isolation", list(report.isolation.values()))


def _preflight(root: Path, stage: str | None) -> int:
    report = run_preflight(root)
    for err in report.static_errors:
        print(f"ERROR: {err}", file=sys.stderr)
    if report.static_errors:
        return 1
    _print_report(report)
    if stage is None:
        return 0
    verdict = evaluate_stage(stage, report)
    if verdict.eligible:
        via = ", ".join(f"{op}={b}" for op, b in verdict.backends.items())
        print(f"stage {stage}: ELIGIBLE ({via})")
        return 0
    print(f"stage {stage}: BLOCKED", file=sys.stderr)
    for reason in verdict.reasons:
        print(f"  - {reason}", file=sys.stderr)
    return 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agentic_pipeline")
    sub = parser.add_subparsers(dest="command", required=True)
    val = sub.add_parser("validate", help="check graph, policy, and model registry invariants")
    val.add_argument("--root", type=Path, default=REPO_ROOT)
    pre = sub.add_parser("preflight", help="read-only host readiness inspection")
    pre.add_argument("--root", type=Path, default=REPO_ROOT)
    pre.add_argument("--stage", help="exit 1 unless this stage is eligible for dispatch")
    args = parser.parse_args(argv)

    if args.command == "preflight":
        return _preflight(args.root, args.stage)
    return _validate(args.root)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
