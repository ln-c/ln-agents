"""`agentic_pipeline run {init,step,status,resume}`: drive one run directory.

Exit codes for `run step`: 0 when the run advanced or stopped normally (finished,
awaiting a human), 1 when blocked (nothing changed), 2 when paused (a human must
resume). Anything that cannot be trusted (invalid state, policy drift, a broken
provenance chain, a second runner) is an error with exit 1.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Iterator, Mapping
from pathlib import Path

from agentic_pipeline.dispatch import BackendAdapter, Dispatcher
from agentic_pipeline.graph import REPO_ROOT, Graph, load_yaml
from agentic_pipeline.provenance import JsonlProvenanceLog
from agentic_pipeline.readiness import run_preflight
from agentic_pipeline.run_state import (
    RunStateError,
    RunStore,
    Status,
    check_policy,
    recover,
    resume,
    validate_run_id,
)
from agentic_pipeline.runner import ChosenGuard, Outcome, init_run, open_run
from agentic_pipeline.transition import BudgetLedger

DEFAULT_RUNS_DIR = REPO_ROOT / ".runs"


class LazyAdapters(Mapping[str, BackendAdapter]):
    """Real adapters, built on first use so blocked steps never probe the host."""

    def __init__(self, registry: Mapping[str, object]) -> None:
        self._registry = registry
        self._built: dict[str, BackendAdapter] | None = None

    def _load(self) -> dict[str, BackendAdapter]:
        if self._built is None:
            from agentic_pipeline.adapters import ClaudeAdapter, PiQwenAdapter

            built: dict[str, BackendAdapter] = {}
            for name, factory in (
                ("qwen_local", PiQwenAdapter.from_registry),
                ("claude_pro", ClaudeAdapter.from_registry),
            ):
                try:
                    built[name] = factory(self._registry)  # type: ignore[arg-type]
                except (LookupError, OSError, ValueError, KeyError):
                    continue  # missing backend: dispatch reports "no adapter"
            self._built = built
        return self._built

    def __getitem__(self, key: str) -> BackendAdapter:
        return self._load()[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._load())

    def __len__(self) -> int:
        return len(self._load())


def _dispatcher_factory(root: Path, run_dir: Path):  # noqa: ANN202
    registry = load_yaml(root / "policy" / "model_registry.yaml")
    graph = Graph.load(root / "pipeline" / "graph.yaml")
    data_classes = load_yaml(root / "policy" / "data_classes.yaml")

    def make(log: JsonlProvenanceLog, ledger: BudgetLedger, guards: ChosenGuard) -> Dispatcher:
        return Dispatcher(
            graph=graph,
            registry=registry,
            log=log,
            ledger=ledger,
            adapters=LazyAdapters(registry),
            preflight=lambda: run_preflight(root),
            run_root=run_dir,
            data_classes=data_classes,
            guards=guards,
        )

    return make


def _init(args: argparse.Namespace, run_dir: Path) -> int:
    state = init_run(args.root, run_dir, args.run_id, args.entry)
    print(f"run {state.run_id} created at {state.current} in {run_dir}")
    return 0


def _status(args: argparse.Namespace, run_dir: Path) -> int:
    state = RunStore(run_dir).load()
    print(f"run:      {state.run_id}")
    print(
        f"status:   {state.status.value}"
        + (f" ({state.paused_reason})" if state.paused_reason else "")
    )
    print(f"at:       {state.current}   steps: {state.steps}")
    if state.entering:
        print(f"entering: {state.entering}")
    print(f"budget:   consumed={state.ledger['consumed']} reserved={state.ledger['reserved']}")
    print(f"resumes:  {len(state.resumes)}")
    return 0


def _step(args: argparse.Namespace, run_dir: Path) -> int:
    runner = open_run(args.root, run_dir, _dispatcher_factory(args.root, run_dir))
    try:
        result = runner.run(args.max_steps)
    finally:
        runner.close()
    print(f"{result.outcome.value}: at {result.node} {result.detail}".rstrip())
    return {Outcome.BLOCKED: 1, Outcome.PAUSED: 2}.get(result.outcome, 0)


def _resume(args: argparse.Namespace, run_dir: Path) -> int:
    store = RunStore(run_dir)
    store.acquire()
    try:
        state = store.load()
        check_policy(state, args.root)
        log = JsonlProvenanceLog(store.provenance_dir, state.run_id)
        recover(state, log)
        resume(state, log, args.by, args.reason)
        store.save(state)
    finally:
        store.release()
    print(
        f"run {state.run_id} resumed by {args.by}; status {Status.RUNNING.value} at {state.current}"
    )
    return 0


def add_parser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    run = sub.add_parser("run", help="drive one pipeline run")
    commands = run.add_subparsers(dest="run_command", required=True)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("run_id")
    common.add_argument("--root", type=Path, default=REPO_ROOT)
    common.add_argument("--runs-dir", type=Path, default=DEFAULT_RUNS_DIR)

    init = commands.add_parser("init", parents=[common], help="create a run at an entrypoint")
    init.add_argument("--entry", default="START")
    step = commands.add_parser("step", parents=[common], help="advance until blocked or stopped")
    step.add_argument("--max-steps", type=int, default=100)
    commands.add_parser("status", parents=[common], help="show run state")
    res = commands.add_parser("resume", parents=[common], help="record a human resume decision")
    res.add_argument("--by", required=True, help="who is resuming (self-asserted, not verified)")
    res.add_argument("--reason", required=True)


def run_command(args: argparse.Namespace) -> int:
    handler = {"init": _init, "step": _step, "status": _status, "resume": _resume}[args.run_command]
    try:
        run_dir = args.runs_dir / validate_run_id(args.run_id)
        return handler(args, run_dir)
    except RunStateError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except ValueError as exc:  # invalid run id etc.
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
