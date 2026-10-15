"""Runner: walks pipeline/graph.yaml one edge at a time, persisting after every step.

The runner never decides anything itself and never trusts a model. At each node:

* a registered deterministic handler may do the node's work and returns the guard
  to take (PR-8/9 register these; none exist yet), or
* a model stage with a single `always` edge takes it, or
* the step is BLOCKED, leaving the run exactly where it was.

Entering a model stage goes through `Dispatcher.dispatch_stage` (all readiness,
data-class, budget, and provenance gates). Entering any other node checks the edge
and charges its budget. Human states stop the run (`awaiting_human`); terminal
states finish it. A step that raises after spending budget pauses the run, and a
paused run only continues through `run_state.resume`.

An edge whose budget is spent is replaced by the node's spent route to the budget's
on_exhausted target (graph rule 9), so an exhausted retry escalates instead of stalling.
Each transition writes a crash marker first, and its `enter` provenance event is
the commit record. Recovery uses the two to tell a stage that finished from one
that was interrupted: the first advances, the second pauses for a human. Resuming
an interrupted transition re-enters its edge, so a budgeted edge is charged again.
Handlers must be idempotent: a crash after a handler ran but before the marker was
written re-runs it.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from agentic_pipeline.approvals import (
    NODE_DECISION,
    ApprovalError,
    Verified,
    store_approval,
    verify_approval,
)
from agentic_pipeline.build_gates import BUILD_GATE_HANDLERS
from agentic_pipeline.dispatch import DispatchBlocked, Dispatcher, RunPaused
from agentic_pipeline.gates import GATE_HANDLERS, approval_evidence
from agentic_pipeline.graph import NO_EDGE_KINDS, REPO_ROOT, Edge, Graph, spent_route
from agentic_pipeline.grounding import (
    ENTRY_SPEC_FILE,
    GROUNDING_HANDLERS,
    INPUTS_DIR,
    GroundingError,
    load_entry_spec,
)
from agentic_pipeline.provenance import GENESIS_HASH, JsonlProvenanceLog
from agentic_pipeline.release import RELEASE_HANDLERS, deploy_evidence, release_evidence
from agentic_pipeline.run_state import (
    PauseCause,
    PolicySnapshot,
    RunState,
    RunStateError,
    RunStore,
    Status,
    check_policy,
    load_policy,
    recover,
    validate_run_id,
)
from agentic_pipeline.transition import (
    BudgetExhausted,
    BudgetLedger,
    TransitionDenied,
    check_transition,
)


def _no_log(event: Mapping[str, Any]) -> dict[str, Any]:
    raise RuntimeError("this context has no provenance log to append to")


@dataclass(frozen=True, slots=True)
class HandlerContext:
    run_id: str
    run_root: Path
    node: str
    graph: Graph
    registry: Mapping[str, Any]
    root: Path = REPO_ROOT  # repository whose pinned snapshot the run grounds
    policy_digests: Mapping[str, str] = field(default_factory=dict)  # frozen at run start
    provenance_tip: Callable[[], str] = lambda: GENESIS_HASH  # chain tip, for manifests
    entered_from: Callable[[], str | None] = lambda: None  # state this node was entered from
    events: Callable[[], list[dict[str, Any]]] = list  # the run's provenance records so far
    append: Callable[[Mapping[str, Any]], dict[str, Any]] = _no_log  # append one provenance event


type StateHandler = Callable[[HandlerContext], str]

# Deterministic per-node handlers: grounding (PR-8), design-side gates (PR-9a), build-side
# gates E2, X, E3 (PR-9b). F1, RM, RC and the rest have none yet, so a run blocks at them.
DEFAULT_HANDLERS: dict[str, StateHandler] = {
    **GROUNDING_HANDLERS,
    **GATE_HANDLERS,
    **BUILD_GATE_HANDLERS,
    **RELEASE_HANDLERS,
}


class Outcome(StrEnum):
    ADVANCED = "advanced"
    BLOCKED = "blocked"
    PAUSED = "paused"
    AWAITING_HUMAN = "awaiting_human"
    FINISHED = "finished"


@dataclass(frozen=True, slots=True)
class StepResult:
    outcome: Outcome
    node: str
    detail: str = ""


class ChosenGuard:
    """Guard evaluator: only the guard a deterministic handler chose may fire."""

    def __init__(self) -> None:
        self.guard: str | None = None

    def __call__(self, edge: Edge, run_context: Mapping[str, Any]) -> bool:
        return self.guard is not None and edge.guard == self.guard


class _Blocked(Exception):
    pass


MakeDispatcher = Callable[
    [PolicySnapshot, JsonlProvenanceLog, BudgetLedger, ChosenGuard], Dispatcher
]


class Runner:
    def __init__(
        self,
        *,
        graph: Graph,
        registry: Mapping[str, Any],
        store: RunStore,
        log: JsonlProvenanceLog,
        ledger: BudgetLedger,
        dispatcher: Dispatcher,
        guards: ChosenGuard,
        state: RunState,
        handlers: Mapping[str, StateHandler] | None = None,
        budgets: Mapping[str, Any] | None = None,
        root: Path | None = None,
    ) -> None:
        self.root = root if root is not None else REPO_ROOT  # repo whose policy is frozen
        self.graph, self.registry, self.store = graph, registry, store
        self.budgets = dict((budgets or {}).get("budgets") or {})
        self.log, self.ledger, self.dispatcher = log, ledger, dispatcher
        self.state, self.guards = state, guards
        self.handlers = dict(DEFAULT_HANDLERS if handlers is None else handlers)
        self.ledger.listener = lambda _ledger: self._save()

    # ── persistence ──────────────────────────────────────────────────────────

    def _save(self) -> None:
        self.state.ledger = self.ledger.snapshot()
        self.store.save(self.state)

    def close(self) -> None:
        self.store.release()

    # ── stepping ─────────────────────────────────────────────────────────────

    def step(self) -> StepResult:
        st = self.state
        if st.status is Status.PAUSED:
            return StepResult(Outcome.PAUSED, st.current, st.paused_reason or "")
        if st.status is Status.AWAITING_HUMAN:
            return StepResult(Outcome.AWAITING_HUMAN, st.current)
        if st.status is Status.FINISHED:
            return StepResult(Outcome.FINISHED, st.current)
        try:
            drifted = load_policy(self.root).digests != st.policy
        except RunStateError as exc:
            return StepResult(Outcome.BLOCKED, st.current, f"cannot read policy: {exc}")
        if drifted:
            return StepResult(
                Outcome.BLOCKED,
                st.current,
                "graph or policy changed during this session; the run will refuse to reopen",
            )

        node = st.current
        if self.graph.states[node].get("kind") in NO_EDGE_KINDS:
            return self._finish(node)
        if node in self.graph.human_states:
            st.status = Status.AWAITING_HUMAN
            try:
                self._save()
            except Exception as exc:
                return self._pause(f"state_save_failed: {exc}", PauseCause.STATE_SAVE_FAILED)
            return StepResult(Outcome.AWAITING_HUMAN, node, "human decision required")

        try:
            guard = self._decide(node)
            edge = self._edge(node, guard)
            self._run_side_effects(node, guard)
        except _Blocked as exc:
            return StepResult(Outcome.BLOCKED, node, str(exc))
        except Exception as exc:  # nothing spent or changed yet: retryable, not a pause
            return StepResult(Outcome.BLOCKED, node, f"handler_error: {type(exc).__name__}: {exc}")

        return self._take(node, guard, edge)

    def _take(
        self,
        node: str,
        guard: str,
        edge: Edge,
        spent_budget: str | None = None,
        decision: Verified | None = None,
    ) -> StepResult:
        """Take one edge: crash marker, gated transition, commit record, save.

        If the edge's budget is spent, the runner itself takes the node's spent route
        to the budget's on_exhausted target (graph rule 9 guarantees one exists). That
        is a deterministic policy route, not a model decision; with no route it pauses.
        """
        st = self.state
        try:
            after_seq = self.log.count()
        except Exception as exc:
            return StepResult(Outcome.BLOCKED, node, f"cannot read provenance log: {exc}")
        st.entering = {
            "from": node,
            "guard": guard,
            "to": edge.to,
            "after_seq": after_seq,
            "consumed_before": self.ledger.snapshot()["consumed"],
        }
        try:
            self._save()  # the crash marker must be durable before anything can be spent
        except Exception as exc:
            st.entering = None
            return StepResult(Outcome.BLOCKED, node, f"cannot persist run state: {exc}")

        self.guards.guard = guard
        context = {"run_id": st.run_id, "guard": guard}
        ledger_before = self.ledger.snapshot()
        try:
            if edge.to in self.registry["stage_requirements"]:
                extra = {"human_decision": decision} if decision is not None else {}
                self.dispatcher.dispatch_stage(node, guard, edge.to, context, **extra)
            else:
                taken = check_transition(
                    self.graph, node, guard, edge.to, self.guards, context, decision
                )
                reservation = self.ledger.reserve(taken)
                if reservation is not None:
                    reservation.commit()
        except (DispatchBlocked, TransitionDenied) as exc:
            st.entering = None
            exhausted = exc if isinstance(exc, BudgetExhausted) else exc.__cause__
            if isinstance(exhausted, BudgetExhausted):
                target = (self.budgets.get(exhausted.budget) or {}).get("on_exhausted")
                route = spent_route(self.graph, node, target) if target else None
                if route is not None and spent_budget is None:
                    self.guards.guard = None
                    return self._take(
                        node, route.guard, route, spent_budget=exhausted.budget, decision=decision
                    )
                return self._pause(
                    f"budget_exhausted: {exhausted.budget} (on_exhausted: {target}); "
                    f"no spent route from {node}",
                    PauseCause.BUDGET_EXHAUSTED,
                )
            self._try_save()  # if this fails, recovery sees an untouched marker and clears it
            return StepResult(Outcome.BLOCKED, node, str(exc))
        except RunPaused as exc:
            st.entering = None
            return self._pause(f"{type(exc).__name__}: {exc}", PauseCause.DISPATCH_PAUSED)
        except Exception as exc:
            st.entering = None
            detail = f"unexpected_error: {type(exc).__name__}: {exc}"
            if self._unchanged_since(ledger_before, after_seq):
                self._try_save()  # nothing spent or logged: safe to retry
                return StepResult(Outcome.BLOCKED, node, detail)
            return self._pause(detail, PauseCause.UNEXPECTED_ERROR)
        finally:
            self.guards.guard = None

        # Commit record. If it cannot be written the marker stays set and the run pauses.
        try:
            self.log.append({"state": edge.to, "event": "enter"})
        except Exception as exc:
            return self._pause(f"provenance_unavailable: {exc}", PauseCause.PROVENANCE_UNAVAILABLE)
        st.current, st.entering = edge.to, None
        st.steps += 1
        try:
            self._save()
        except Exception as exc:
            # Disk still holds the marker; recovery finds the enter event and advances.
            return self._pause(f"state_save_failed: {exc}", PauseCause.STATE_SAVE_FAILED)
        how = f"budget {spent_budget} spent: " if spent_budget else ""
        return StepResult(Outcome.ADVANCED, edge.to, f"{how}{node} --{guard}--> {edge.to}")

    def run(self, max_steps: int = 100) -> StepResult:
        """Step until the run stops advancing or `max_steps` is reached."""
        result = StepResult(Outcome.BLOCKED, self.state.current, "no steps taken")
        for _ in range(max_steps):
            result = self.step()
            if result.outcome is not Outcome.ADVANCED:
                break
        return result

    # ── human decisions ──────────────────────────────────────────────────────

    def approval_evidence(self) -> dict[str, Any]:
        """The evidence a human signs at the current human state, recomputed from sealed state."""
        st = self.state
        if st.status is not Status.AWAITING_HUMAN:
            raise ApprovalError(f"run is {st.status.value}; a decision needs awaiting_human")
        if st.current not in NODE_DECISION:
            raise ApprovalError(f"signed approvals are not wired for {st.current} yet")
        policy = load_policy(self.root)
        check_policy(st, policy)
        try:
            gather = {"H1": approval_evidence, "R1": release_evidence, "R2": deploy_evidence}[
                st.current
            ]
            evidence = gather(self._context(st.current), policy)
        except Exception as exc:  # unverifiable evidence is never approvable
            raise ApprovalError(
                f"cannot recompute the evidence: {type(exc).__name__}: {exc}"
            ) from exc
        return {**evidence, "provenance_tip": self.log.tip(), "policy": dict(st.policy)}

    def decide(self, document: bytes, signature: str) -> StepResult:
        """Take the human edge named by a verified signed approval; otherwise change nothing."""
        st = self.state
        node = st.current
        evidence = self.approval_evidence()
        policy = load_policy(self.root)
        verified: Verified = verify_approval(
            document,
            signature,
            approvers=policy.approvers,
            run_id=st.run_id,
            node=node,
            guards=[e.guard for e in self.graph.edges if e.src == node],
            evidence=evidence,
        )
        edge = self._edge(node, verified.guard)
        st.status = Status.RUNNING
        try:
            store_approval(self.store.run_root, document, signature)
            self.log.append(
                {
                    "state": node,
                    "event": "approval",
                    "approver_id": verified.approver,
                    "approved_sha": verified.approved_sha,
                    "approval_digest": verified.digest,
                    "reason_code": verified.guard,
                }
            )
        except Exception as exc:
            st.status = Status.AWAITING_HUMAN
            return StepResult(Outcome.BLOCKED, node, f"cannot record the approval: {exc}")
        result = self._take(node, verified.guard, edge, decision=verified)
        if result.outcome is Outcome.BLOCKED:
            st.status = Status.AWAITING_HUMAN  # the approval is spent; a new one is needed
            self._try_save()
        return result

    # ── helpers ──────────────────────────────────────────────────────────────

    def _context(self, node: str) -> HandlerContext:
        return HandlerContext(
            self.state.run_id,
            self.store.run_root,
            node,
            self.graph,
            self.registry,
            root=self.root,
            policy_digests=self.state.policy,
            provenance_tip=self.log.tip,
            entered_from=self._entered_from,
            events=self.log.records,
            append=self.log.append,
        )

    def _decide(self, node: str) -> str:
        handler = self.handlers.get(node)
        if handler is not None:
            return handler(self._context(node))
        outgoing = [e for e in self.graph.edges if e.src == node]
        if (
            node in self.registry["stage_requirements"]
            and len(outgoing) == 1
            and outgoing[0].guard == "always"
        ):
            return "always"
        raise _Blocked(f"no handler registered for {node}")

    def _run_side_effects(self, node: str, guard: str) -> None:
        """Run the states a node lists as side effects for this guard, before it transitions.

        They are idempotent handlers that never take control. A missing or failing one blocks
        the step, so the transition does not happen without them.
        """
        for when, run in self.graph.side_effects.get(node, []):
            if when != guard:
                continue
            handler = self.handlers.get(run)
            if handler is None:
                raise _Blocked(f"no handler registered for side effect {run}")
            handler(self._context(run))

    def _entered_from(self) -> str | None:
        """The state before the latest entry into the current node, from committed enters."""
        entered = [r["state"] for r in self.log.records() if r["event"] == "enter"]
        if not entered or entered[-1] != self.state.current or len(entered) < 2:
            return None
        return entered[-2]

    def _edge(self, node: str, guard: str) -> Edge:
        matches = [e for e in self.graph.edges if e.src == node and e.guard == guard]
        if not matches:
            raise _Blocked(f"{node} has no edge with guard {guard!r}")
        if len(matches) > 1:
            raise _Blocked(f"{node} has {len(matches)} edges with guard {guard!r}")
        return matches[0]

    def _unchanged_since(self, ledger_before: dict[str, dict[str, int]], seq: int) -> bool:
        """True only if provably nothing was reserved, spent, or logged."""
        try:
            return self.ledger.snapshot() == ledger_before and self.log.count() == seq
        except Exception:
            return False

    def _finish(self, node: str) -> StepResult:
        try:
            # A run reaches a terminal node once, so any earlier terminal event counts.
            if not any(r["event"] == "terminal" and r["state"] == node for r in self.log.records()):
                self.log.append({"state": node, "event": "terminal"})
        except Exception as exc:
            return self._pause(f"provenance_unavailable: {exc}", PauseCause.PROVENANCE_UNAVAILABLE)
        self.state.status = Status.FINISHED
        try:
            self._save()
        except Exception as exc:  # the terminal event is not repeated on the next attempt
            return self._pause(f"state_save_failed: {exc}", PauseCause.STATE_SAVE_FAILED)
        return StepResult(Outcome.FINISHED, node)

    def _pause(self, reason: str, cause: PauseCause) -> StepResult:
        self.state.pause(reason, cause)
        self._try_save()  # if this fails, the saved crash marker still forces a pause
        return StepResult(Outcome.PAUSED, self.state.current, reason)

    def _try_save(self) -> None:
        with contextlib.suppress(Exception):
            self._save()


# ── lifecycle ────────────────────────────────────────────────────────────────


def init_run(
    root: Path, run_dir: Path, run_id: str, entry: str, entry_spec: Path | None = None
) -> RunState:
    """Create a new run at an entrypoint, freezing graph and policy digests.

    `entry_spec` is the grounding agent's payload; it is validated here so a bad one never
    creates a run, and copied into the run for START to seal. START validates it again.
    """
    validate_run_id(run_id)
    policy = load_policy(root)
    if entry not in policy.graph.entrypoints:
        raise RunStateError(f"{entry!r} is not an entrypoint {policy.graph.entrypoints}")
    spec_bytes = None
    if entry_spec is not None:
        try:
            spec_bytes = entry_spec.read_bytes()
            load_entry_spec(spec_bytes)  # schema only; START checks the class and handoff
        except (OSError, GroundingError) as exc:
            raise RunStateError(f"cannot use entry spec {entry_spec}: {exc}") from exc
    store = RunStore(run_dir)
    store.acquire()
    try:
        store.provenance_dir.mkdir(parents=True, exist_ok=True)
        state = RunState(run_id=run_id, current=entry, policy=policy.digests)
        store.create(state)
        if spec_bytes is not None:
            target = run_dir / INPUTS_DIR / ENTRY_SPEC_FILE
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(spec_bytes)
        JsonlProvenanceLog(store.provenance_dir, run_id).append({"state": entry, "event": "enter"})
    finally:
        store.release()
    return state


def open_run(
    root: Path,
    run_dir: Path,
    make_dispatcher: MakeDispatcher,
    handlers: Mapping[str, StateHandler] | None = None,
) -> Runner:
    """Lock and load a run, refuse on policy drift, and apply fail-closed recovery."""
    store = RunStore(run_dir)
    store.acquire()
    try:
        state = store.load()
        policy = load_policy(root)  # one read; everything below uses these checked bytes
        check_policy(state, policy)
        try:
            ledger = BudgetLedger.from_policy(policy.budgets)
        except (KeyError, TypeError, AttributeError) as exc:
            raise RunStateError(f"malformed policy/budgets.yaml: {exc}") from exc
        store.provenance_dir.mkdir(parents=True, exist_ok=True)
        log = JsonlProvenanceLog(store.provenance_dir, state.run_id)
        if recover(state, log):
            store.save(state)
        ledger.consumed = dict(state.ledger["consumed"])
        guards = ChosenGuard()
        dispatcher = make_dispatcher(policy, log, ledger, guards)
        return Runner(
            graph=policy.graph,
            registry=policy.registry,
            store=store,
            log=log,
            ledger=ledger,
            dispatcher=dispatcher,
            guards=guards,
            state=state,
            handlers=handlers,
            budgets=policy.budgets,
            root=root,
        )
    except BaseException:
        store.release()
        raise
