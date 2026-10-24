"""Command line: `python -m agentic_pipeline {validate,preflight,models,run,serve} ...`."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from agentic_pipeline.graph import REPO_ROOT
from agentic_pipeline.models_cli import add_parser as add_models_parser
from agentic_pipeline.models_cli import models_command
from agentic_pipeline.readiness import Check, Report, evaluate_stage, run_preflight
from agentic_pipeline.run_cli import DEFAULT_RUNS_DIR, run_command
from agentic_pipeline.run_cli import add_parser as add_run_parser
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


def _add_serve_parser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    serve = sub.add_parser("serve", help="serve the pipeline to other agents")
    kinds = serve.add_subparsers(dest="serve_command", required=True)
    a2a = kinds.add_parser("a2a", help="A2A server on loopback (needs the `a2a` extra)")
    a2a.add_argument("--root", type=Path, default=REPO_ROOT)
    a2a.add_argument("--runs-dir", type=Path, default=DEFAULT_RUNS_DIR)
    a2a.add_argument("--host", default="127.0.0.1", help="a literal loopback address")
    a2a.add_argument("--port", type=int, default=8420)
    a2a.add_argument(
        "--token-file",
        type=Path,
        help="bearer token (created 0600 if missing); default: <runs-dir>/.a2a/token",
    )
    mcp = kinds.add_parser(
        "mcp",
        help="local project MCP server on stdio, with the tools of one profile "
        "(needs the `mcp` extra)",
    )
    mcp.add_argument("--profile", help="a profile of policy/tools.yaml (needed without --run)")
    mcp.add_argument("--root", type=Path, default=REPO_ROOT, help="the project to serve")
    mcp.add_argument("--runs-dir", type=Path, default=DEFAULT_RUNS_DIR)
    mcp.add_argument(
        "--run",
        help="bind the session to this run: its pinned snapshot, its frozen policy; needs "
        "--operation",
    )
    mcp.add_argument(
        "--operation", help="STAGE.OP of the run, such as E1.implementation_patch; needs --run"
    )
    mcp.add_argument(
        "--backend",
        help="the model the operation runs on; refused if it is outside the host and the "
        "profile's destination is inside it",
    )


def _serve_mcp(args: argparse.Namespace) -> int:
    # Imported here so the core install works without the optional `mcp` extra.
    try:
        from agentic_pipeline.mcp_policy import ToolsPolicyError
        from agentic_pipeline.mcp_server import serve
    except ImportError as exc:
        print(
            f"ERROR: the MCP server needs the `mcp` extra ({exc.name} missing): "
            "uv sync --extra mcp",
            file=sys.stderr,
        )
        return 1
    if (args.run is None) != (args.operation is None):
        print("ERROR: --run and --operation go together", file=sys.stderr)
        return 1
    if args.run is None and args.profile is None:
        print("ERROR: name a profile with --profile, or bind to a run with --run", file=sys.stderr)
        return 1
    try:
        serve(
            args.root,
            args.runs_dir,
            args.profile,
            run=args.run,
            operation=args.operation,
            backend=args.backend,
        )
    except (ToolsPolicyError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


def _serve(args: argparse.Namespace) -> int:
    if args.serve_command == "mcp":
        return _serve_mcp(args)
    # Imported here so the core install works without the optional `a2a` extra.
    try:
        from agentic_pipeline.a2a_server import ServeError, serve
    except ImportError as exc:
        print(
            f"ERROR: the A2A server needs the `a2a` extra ({exc.name} missing): "
            "uv sync --extra a2a",
            file=sys.stderr,
        )
        return 1
    token_file = args.token_file or args.runs_dir / ".a2a" / "token"
    try:
        serve(args.root, args.runs_dir, args.host, args.port, token_file)
    except (ServeError, OSError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agentic_pipeline")
    sub = parser.add_subparsers(dest="command", required=True)
    val = sub.add_parser("validate", help="check graph, policy, and model registry invariants")
    val.add_argument("--root", type=Path, default=REPO_ROOT)
    pre = sub.add_parser("preflight", help="read-only host readiness inspection")
    pre.add_argument("--root", type=Path, default=REPO_ROOT)
    pre.add_argument("--stage", help="exit 1 unless this stage is eligible for dispatch")
    add_models_parser(sub)
    add_run_parser(sub)
    _add_serve_parser(sub)
    args = parser.parse_args(argv)

    if args.command == "serve":
        return _serve(args)
    if args.command == "run":
        return run_command(args)
    if args.command == "models":
        return models_command(args)
    if args.command == "preflight":
        return _preflight(args.root, args.stage)
    return _validate(args.root)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
