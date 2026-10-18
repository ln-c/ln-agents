"""`agentic_pipeline run {init,step,status,resume,approve,decide}`: drive one run directory.

Exit codes for `run step`: 0 when the run advanced or stopped normally (finished,
awaiting a human), 1 when blocked (nothing changed), 2 when paused (a human must
resume). Anything that cannot be trusted (invalid state, policy drift, a broken
provenance chain, a second runner) is an error with exit 1.

A human state (H1) moves only by a signed approval: `run approve` writes the document to
sign, the human signs it with `ssh-keygen -Y sign`, and `run decide` hands the signature to
the runner, which verifies it (see approvals.py). `resume` only continues a paused run.
"""

from __future__ import annotations

import argparse
import copy
import sys
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agentic_pipeline.approvals import (
    REMEDIATION_CAUSES,
    ApprovalError,
    build_document,
    signing_command,
)
from agentic_pipeline.dispatch import BackendAdapter, Dispatcher
from agentic_pipeline.graph import REPO_ROOT
from agentic_pipeline.provenance import JsonlProvenanceLog, verify_provenance_chain
from agentic_pipeline.readiness import run_preflight
from agentic_pipeline.run_state import (
    PauseCause,
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


def _adapter_factories() -> dict[str, Callable[[Mapping[str, Any], str], BackendAdapter]]:
    """Adapter constructors by provider id; each is called with the registry and a model id."""
    from agentic_pipeline.adapters import (
        AgyAdapter,
        ClaudeAdapter,
        DecisionAdapter,
        PiQwenAdapter,
    )

    return {
        "llamacpp_local": PiQwenAdapter.from_registry,  # type: ignore[dict-item]
        "llamacpp_decision": DecisionAdapter.from_registry,  # type: ignore[dict-item]
        "openvino_local": PiQwenAdapter.from_registry,  # type: ignore[dict-item]
        "claude_pro": ClaudeAdapter.from_registry,  # type: ignore[dict-item]
        "google_ai_pro": AgyAdapter.from_registry,  # type: ignore[dict-item]
    }


class LazyAdapters(Mapping[str, BackendAdapter]):
    """Real adapters, each built only when its backend is first needed.

    A Qwen-only stage never constructs (or probes credentials for) the Claude adapter.
    """

    def __init__(self, registry: Mapping[str, Any]) -> None:
        self._registry = registry
        self._built: dict[str, BackendAdapter] = {}
        self._failed: dict[str, str] = {}

    def _build(self, name: str) -> BackendAdapter | None:
        if name in self._built or name in self._failed:
            return self._built.get(name)
        model = self._registry["models"].get(name)
        factory = _adapter_factories().get(model["provider"]) if model else None
        if factory is None:
            return None
        try:
            self._built[name] = factory(self._registry, name)
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
        return (name for name in self._registry["models"] if self._build(name) is not None)

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
    state = init_run(args.root, run_dir, args.run_id, args.entry, args.entry_spec)
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
        + (f" [{state.pause_cause.value}] ({state.paused_reason})" if state.pause_cause else "")
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


@dataclass(frozen=True, slots=True)
class ApprovalDraft:
    """An unsigned approval document written to disk, with what the approver needs to see."""

    node: str
    evidence: dict[str, Any]
    path: Path
    namespace: str  # the ssh-keygen -Y namespace to sign it under


def write_approval_document(
    root: Path,
    run_dir: Path,
    *,
    decision: str,
    approver: str,
    reason: str,
    candidate: str | None = None,
    remediation_cause: str | None = None,
    out: Path | None = None,
) -> ApprovalDraft:
    """Write the approval document a human then signs; nothing about the run changes.

    Shared by `run approve` and the A2A `approval_document` skill. It never signs or decides.
    """
    runner = open_run(root, run_dir, _dispatcher_factory(root, run_dir))
    try:
        node = runner.state.current
        evidence = runner.approval_evidence()
        document = build_document(
            run_id=runner.state.run_id,
            node=node,
            guard=decision,
            approver=approver,
            candidate=candidate,
            reason=reason,
            evidence=evidence,
            issued_at=datetime.now(UTC).isoformat(),
            remediation_cause=remediation_cause,
        )
        namespace = load_policy(root).approvers["namespace"]
    finally:
        runner.close()
    path = out or run_dir / "approvals" / f"{node}-{decision}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(document)
    return ApprovalDraft(node, evidence, path, namespace)


def _approve(args: argparse.Namespace, run_dir: Path) -> int:
    """Write the approval document for the human to sign; nothing about the run changes."""
    draft = write_approval_document(
        args.root,
        run_dir,
        decision=args.decision,
        approver=args.approver,
        reason=args.reason,
        candidate=args.candidate,
        remediation_cause=args.remediation_cause,
        out=args.out,
    )
    node, evidence, out, namespace = draft.node, draft.evidence, draft.path, draft.namespace
    print(f"approval for {args.run_id} at {node}: {args.decision}")
    if node == "R2":
        print(f"  merged SHA {evidence['merged_sha']} on {evidence['target_branch']}")
        print(f"  deployment environments: {evidence['environments'] or 'none configured'}")
    elif node == "R1":
        print(
            f"  tested base {evidence['commit_sha']}; {evidence['target_branch']} tip "
            f"{evidence['target_base_sha']}"
        )
        print(f"  candidate tree {evidence['candidate_tree']} (patch {evidence['patch']})")
        print(f"  F1 for this patch and base: {evidence['f1_guard']}")
    else:
        print(f"  candidates that survived B1: {evidence['survivors'] or 'none'}")
        for candidate, vetoes in evidence["fired"].items():
            pending = evidence["undetermined"][candidate]
            print(f"  {candidate}: fired {vetoes or '-'}; undetermined {pending or '-'}")
        if evidence["unevaluated"]:
            print(f"  vetoes with no evaluator: {evidence['unevaluated']}")
        for critique, ratio in evidence["valid_ratios"].items():
            share = "no findings" if ratio is None else f"{ratio:.0%} of its findings judged valid"
            print(f"  {critique}: {share}")
        for waived in evidence["waived_blocking"]:
            print(
                f"  waived blocking finding {waived['finding_id']} of {waived['critique']} "
                f"(p={waived['probability']:.3f}); S did not have to answer it"
            )
    print(f"written: {out}")
    print("Review it, then sign it as the approver (writes the .sig file next to it):")
    print(f"  {signing_command(out, namespace)}")
    print(f"then: run decide {args.run_id} --approval {out}")
    return 0


def _decide(args: argparse.Namespace, run_dir: Path) -> int:
    signature_path = args.signature or args.approval.with_name(args.approval.name + ".sig")
    try:
        document = args.approval.read_bytes()
        signature = signature_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ApprovalError(f"cannot read the approval or its signature: {exc}") from exc
    runner = open_run(args.root, run_dir, _dispatcher_factory(args.root, run_dir))
    try:
        result = runner.decide(document, signature)
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
        if state.pause_cause is PauseCause.BUDGET_EXHAUSTED:
            print(
                f"ERROR: {state.paused_reason}. Resuming would retry the same exhausted "
                "edge; the graph needs a spent route (rule 9) or a new run.",
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
    init.add_argument(
        "--entry-spec", type=Path, help="grounding agent's entry_spec JSON (required for START)"
    )
    step = commands.add_parser("step", parents=[common], help="advance until blocked or stopped")
    step.add_argument("--max-steps", type=_positive_int, default=100)
    commands.add_parser("status", parents=[common], help="show run state")
    approve = commands.add_parser(
        "approve", parents=[common], help="write the approval document a human then signs"
    )
    approve.add_argument("--decision", required=True, help="the outgoing guard to take")
    approve.add_argument("--approver", required=True, help="id listed in policy/approvers.yaml")
    approve.add_argument("--reason", required=True)
    approve.add_argument("--candidate", help="the surviving candidate (design approval only)")
    approve.add_argument(
        "--remediation-cause",
        choices=REMEDIATION_CAUSES,
        help="why the merge is denied (R1 deny only); RM routes on it",
    )
    approve.add_argument("--out", type=Path, help="where to write the document")
    decide = commands.add_parser(
        "decide", parents=[common], help="take the edge named by a signed approval"
    )
    decide.add_argument("--approval", type=Path, required=True)
    decide.add_argument("--signature", type=Path, help="default: the approval path plus .sig")
    res = commands.add_parser(
        "resume",
        parents=[common],
        help="record a human resume decision (interactive terminal only)",
    )
    res.add_argument("--by", required=True, help="who is resuming (self-asserted, not verified)")
    res.add_argument("--reason", required=True)


def run_command(args: argparse.Namespace) -> int:
    handler = {
        "init": _init,
        "step": _step,
        "status": _status,
        "resume": _resume,
        "approve": _approve,
        "decide": _decide,
    }[args.run_command]
    try:
        run_dir = args.runs_dir / validate_run_id(args.run_id)
        return handler(args, run_dir)
    except (RunStateError, ApprovalError, ValueError) as exc:  # ValueError: e.g. bad run id
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
