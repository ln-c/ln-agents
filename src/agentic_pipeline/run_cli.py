"""`agentic_pipeline run {init,step,status,resume}`: drive one run directory.

Exit codes for `run step`: 0 when the run advanced or stopped normally (finished,
awaiting a human), 1 when blocked (nothing changed), 2 when paused (a human must
resume). Anything that cannot be trusted (invalid state, policy drift, a broken
provenance chain, a second runner) is an error with exit 1.
"""

from __future__ import annotations

import argparse
import copy
import sys
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path

from agentic_pipeline.dispatch import BackendAdapter, Dispatcher
from agentic_pipeline.graph import REPO_ROOT
from agentic_pipeline.provenance import JsonlProvenanceLog, verify_provenance_chain
from agentic_pipeline.readiness import run_preflight
from agentic_pipeline.run_state import (
    PolicySnapshot,
    RunStateError,
    RunStore,
    Status,
    check_policy,
    load_policy,
    recover,
    resume,
    validate_run_id,
)
from agentic_pipeline.runner import ChosenGuard, MakeDispatcher, Outcome, init_run, open_run
from agentic_pipeline.transition import BudgetLedger

DEFAULT_RUNS_DIR = REPO_ROOT / ".runs"


def _adapter_factories() -> dict[str, Callable[[Mapping[str, object]], BackendAdapter]]:
    from agentic_pipeline.adapters import ClaudeAdapter, PiQwenAdapter

    return {
        "qwen_local": PiQwenAdapter.from_registry,  # type: ignore[dict-item]
        "claude_pro": ClaudeAdapter.from_registry,  # type: ignore[dict-item]
    }


class LazyAdapters(Mapping[str, BackendAdapter]):
    """Real adapters, each built only when its backend is first needed.

    A Qwen-only stage never constructs (or probes credentials for) the Claude adapter.
    """

    def __init__(self, registry: Mapping[str, object]) -> None:
        self._registry = registry
        self._built: dict[str, BackendAdapter] = {}
        self._failed: dict[str, str] = {}

    def _build(self, name: str) -> BackendAdapter | None:
        if name in self._built or name in self._failed:
            return self._built.get(name)
        factory = _adapter_factories().get(name)
        if factory is None:
            return None
        try:
            self._built[name] = factory(self._registry)
        except Exception as exc:  # any failure: dispatch blocks with this reason
            self._failed[name] = f"{type(exc).__name__}: {exc}"
        return self._built.get(name)

    def unavailable(self, name: str) -> str | None:
        """Why `name` could not be built, if it could not."""
        self._build(name)
        return self._failed.get(name)

    def __getitem__(self, key: str) -> BackendAdapter:
        adapter = self._build(key)
        if adapter is None:
            raise KeyError(key)
        return adapter

    def __iter__(self) -> Iterator[str]:  # builds everything; dispatch never iterates
        return (name for name in _adapter_factories() if self._build(name) is not None)

    def __len__(self) -> int:
        return sum(1 for _ in self)


def _dispatcher_factory(root: Path, run_dir: Path) -> MakeDispatcher:
    def make(
        policy: PolicySnapshot, log: JsonlProvenanceLog, ledger: BudgetLedger, guards: ChosenGuard
    ) -> Dispatcher:
        return Dispatcher(
            graph=policy.graph,
            registry=policy.registry,
            log=log,
            ledger=ledger,
            adapters=LazyAdapters(policy.registry),
            preflight=lambda: run_preflight(root, registry=policy.registry),
            run_root=run_dir,
            data_classes=policy.data_classes,
            guards=guards,
        )

    return make


def _init(args: argparse.Namespace, run_dir: Path) -> int:
    state = init_run(args.root, run_dir, args.run_id, args.entry)
    print(f"run {state.run_id} created at {state.current} in {run_dir}")
    return 0


def _status(args: argparse.Namespace, run_dir: Path) -> int:
    store = RunStore(run_dir)
    state = store.load()
    active = store.is_locked()
    # Read-only preview of what the next open would do (recovery runs on a copy). A
    # live runner's marker and reservations are not a crash, so skip it while one runs.
    preview = copy.deepcopy(state)
    pending: list[str] = []
    if not active:
        try:
            pending = recover(preview, JsonlProvenanceLog(store.provenance_dir, state.run_id))
        except RunStateError as exc:
            pending = [f"WILL REFUSE TO OPEN: {exc}"]
    try:
        check_policy(state, load_policy(args.root))
    except RunStateError as exc:
        pending.append(f"WILL REFUSE TO OPEN: {exc}")
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
    if active:
        print("runner:   active (recovery preview skipped)")
    if preview.status is not state.status or preview.current != state.current:
        why = f" ({preview.paused_reason})" if preview.paused_reason else ""
        print(f"on open:  {preview.status.value}{why} at {preview.current}")
    for note in pending:
        print(f"recovery: {note}")
    return 0


def _step(args: argparse.Namespace, run_dir: Path) -> int:
    runner = open_run(args.root, run_dir, _dispatcher_factory(args.root, run_dir))
    try:
        result = runner.run(args.max_steps)
    finally:
        runner.close()
    print(f"{result.outcome.value}: at {result.node} {result.detail}".rstrip())
    return {Outcome.BLOCKED: 1, Outcome.PAUSED: 2}.get(result.outcome, 0)


def _confirm_human(run_id: str) -> bool:
    """Interim human check (until signed approvals): a real terminal, run id retyped.

    This is a barrier against non-interactive callers such as a model's shell tool,
    not authentication; anything that drives a terminal can still pass it.
    """
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        print("ERROR: resume must be run by a human at an interactive terminal", file=sys.stderr)
        return False
    try:
        typed = input(f"Type the run id ({run_id}) to confirm the resume: ").strip()
    except EOFError:
        return False
    return typed == run_id


def _resume(args: argparse.Namespace, run_dir: Path) -> int:
    """Resume only a run that was already paused on disk, after the human sees why.

    If recovery would change the run (a crash that `run status` had not shown yet),
    the recovered state is saved and the resume is refused, so nobody signs off on
    model calls they have not been shown.
    """
    store = RunStore(run_dir)
    store.acquire()
    try:
        state = store.load()
        check_policy(state, load_policy(args.root))
        log = JsonlProvenanceLog(store.provenance_dir, state.run_id)
        notes = recover(state, log)
        if notes:
            store.save(state)
            print(
                f"ERROR: recovery changed this run; now {state.status.value} "
                f"({state.paused_reason}). Review these, then resume again:",
                file=sys.stderr,
            )
            for note in notes:
                print(f"  - {note}", file=sys.stderr)
            return 1
        if state.status is not Status.PAUSED:
            print(f"ERROR: run is {state.status.value}, not paused", file=sys.stderr)
            return 1
        if (state.paused_reason or "").startswith("budget_exhausted"):
            print(
                f"ERROR: {state.paused_reason}. Resuming would retry the same exhausted "
                "edge. It needs a *_spent route chosen by a deterministic handler, or a "
                "human decision at H1.",
                file=sys.stderr,
            )
            return 1
        unresolved = verify_provenance_chain(log.path).unresolved_calls if log.path.exists() else {}
        print(f"run {state.run_id} is paused at {state.current}: {state.paused_reason}")
        for invocation_id, call in sorted(unresolved.items()):
            print(
                f"  unresolved model call {invocation_id} ({call['state']}."
                f"{call['operation_id']}) will be marked human_reconciled"
            )
        if not _confirm_human(args.run_id):
            print("resume not confirmed; nothing changed", file=sys.stderr)
            return 1
        resume(state, log, args.by, args.reason)
        store.save(state)
    finally:
        store.release()
    print(f"run {state.run_id} resumed by {args.by} (self-asserted); running at {state.current}")
    return 0


def _positive_int(text: str) -> int:
    value = int(text)
    if value < 1:
        raise argparse.ArgumentTypeError("must be a positive integer")
    return value


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
    step.add_argument("--max-steps", type=_positive_int, default=100)
    commands.add_parser("status", parents=[common], help="show run state")
    res = commands.add_parser(
        "resume",
        parents=[common],
        help="record a human resume decision (interactive terminal only)",
    )
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
