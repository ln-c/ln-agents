"""Single dispatch bottleneck for model stages.

Order of operations for `dispatch_stage` (failure handling in brackets):

1. Preflight + stage eligibility: static validity, credential denylist,
   backend readiness, isolation profile          [DispatchBlocked, no budget]
2. Adapter present for every selected backend    [DispatchBlocked, no budget]
3. Graph edge exists and guard holds             [DispatchBlocked, no budget]
4. Reserve one unit of the edge's budget         [DispatchBlocked if exhausted]
5. Per operation:
   a. re-probe readiness (after the first)       [RunPaused: readiness_lost]
   b. adapter.prepare (no external call)         [release budget if nothing logged]
   c. append + fsync model_call                  [release budget if first call]
   d. commit the reservation (first call only)   [RunPaused: budget_commit_failed]
   e. adapter.invoke                             [exception event; RunPaused]
   f. append + fsync model_result                [ProvenanceIncomplete]
   g. non-ok result (quota, auth, error)         [RunPaused; no in-flight fallback]

Outcomes are provisional: no stage manifest is published yet, so the
orchestrator must not advance state on them.
"""

from __future__ import annotations

import hashlib
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from agentic_pipeline.graph import Graph
from agentic_pipeline.provenance import JsonlProvenanceLog
from agentic_pipeline.readiness import Report, evaluate_stage
from agentic_pipeline.registry import operations
from agentic_pipeline.transition import (
    BudgetLedger,
    GuardEvaluator,
    Reservation,
    TransitionDenied,
    check_transition,
    deny_all_guards,
)


class DispatchBlocked(Exception):
    """Refused before any model_call was logged; no budget was consumed."""


class RunPaused(Exception):
    """Stopped after budget was committed; resuming requires a human (ADR 0003)."""


class ProvenanceIncomplete(RunPaused):
    """A model call happened but its outcome could not be recorded."""


@dataclass(frozen=True, slots=True)
class DispatchGrant:
    """Everything one invocation is allowed to use; adapters receive nothing else."""

    run_id: str
    stage: str
    operation_id: str
    backend: str
    invocation_id: str
    allowed_reads: tuple[str, ...]
    write_root: str


@dataclass(frozen=True, slots=True)
class PreparedCall:
    model: str
    model_version: str
    access_method: str
    prompt: bytes

    @property
    def prompt_hash(self) -> str:
        return "sha256:" + hashlib.sha256(self.prompt).hexdigest()


type ResultStatus = Literal["ok", "quota_exhausted", "auth_failed", "error"]


@dataclass(frozen=True, slots=True)
class AdapterResult:
    status: ResultStatus
    detail: str = ""


class BackendAdapter(Protocol):
    def prepare(self, grant: DispatchGrant) -> PreparedCall:
        """Resolve model/version and build the prompt. Must not call out."""
        ...

    def invoke(self, grant: DispatchGrant, call: PreparedCall) -> AdapterResult: ...


@dataclass(frozen=True, slots=True)
class StageOutcome:
    stage: str
    results: dict[str, AdapterResult]
    provisional: bool = True


def _new_invocation_id() -> str:
    return uuid.uuid4().hex


@dataclass(slots=True)
class Dispatcher:
    graph: Graph
    registry: Mapping[str, Any]
    log: JsonlProvenanceLog
    ledger: BudgetLedger
    adapters: Mapping[str, BackendAdapter]
    preflight: Callable[[], Report]
    guards: GuardEvaluator = deny_all_guards
    new_invocation_id: Callable[[], str] = _new_invocation_id
    _committed: bool = field(default=False, init=False)

    def dispatch_stage(
        self, src: str, guard: str, stage: str, run_context: Mapping[str, Any]
    ) -> StageOutcome:
        verdict = evaluate_stage(stage, self.preflight())
        if not verdict.eligible:
            raise DispatchBlocked(f"{stage}: " + "; ".join(verdict.reasons))
        if missing := sorted(set(verdict.backends.values()) - self.adapters.keys()):
            raise DispatchBlocked(f"{stage}: no adapter for {missing}")
        try:
            edge = check_transition(self.graph, src, guard, stage, self.guards, run_context)
            reservation = self.ledger.reserve(edge)
        except TransitionDenied as exc:
            raise DispatchBlocked(str(exc)) from exc

        self._committed = False
        results: dict[str, AdapterResult] = {}
        spec = self.registry["stage_requirements"][stage]
        for index, (_, op) in enumerate(operations({stage: spec})):
            backend = verdict.backends[op["id"]]
            if index > 0:
                again = evaluate_stage(stage, self.preflight())
                if not again.eligible or again.backends.get(op["id"]) != backend:
                    self._log_exception(stage, op["id"], None, "readiness_lost")
                    raise RunPaused(f"{stage}.{op['id']}: readiness lost before invocation")
            grant = DispatchGrant(
                run_id=self.log.run_id,
                stage=stage,
                operation_id=op["id"],
                backend=backend,
                invocation_id=self.new_invocation_id(),
                allowed_reads=tuple(op["allowed_reads"]),
                write_root=op["write_root"],
            )
            results[op["id"]] = self._invoke(grant, reservation)
        return StageOutcome(stage, results)

    def _invoke(self, grant: DispatchGrant, reservation: Reservation | None) -> AdapterResult:
        adapter = self.adapters[grant.backend]
        correlation = {
            "state": grant.stage,
            "backend": grant.backend,
            "operation_id": grant.operation_id,
            "invocation_id": grant.invocation_id,
        }
        try:
            call = adapter.prepare(grant)
            correlation["access_method"] = call.access_method
            self.log.append(
                {
                    **correlation,
                    "event": "model_call",
                    "model": call.model,
                    "model_version": call.model_version,
                    "prompt_hash": call.prompt_hash,
                }
            )
        except Exception as exc:
            if self._committed:
                raise RunPaused(f"{grant.operation_id}: pre-call step failed: {exc}") from exc
            if reservation is not None:
                reservation.release()
            raise DispatchBlocked(f"{grant.operation_id}: pre-call step failed: {exc}") from exc

        if not self._committed:
            try:
                if reservation is not None:
                    reservation.commit()
            except Exception as exc:
                self._log_exception(
                    grant.stage, grant.operation_id, correlation, "budget_commit_failed"
                )
                raise RunPaused(f"{grant.operation_id}: budget commit failed") from exc
            self._committed = True

        try:
            result = adapter.invoke(grant, call)
        except Exception as exc:
            self._log_exception(grant.stage, grant.operation_id, correlation, "adapter_error")
            raise RunPaused(f"{grant.operation_id}: adapter failed: {exc}") from exc

        try:
            self.log.append({**correlation, "event": "model_result", "reason_code": result.status})
        except Exception as exc:
            raise ProvenanceIncomplete(f"{grant.operation_id}: result not recorded") from exc
        if result.status != "ok":
            raise RunPaused(f"{grant.operation_id}: {result.status} {result.detail}".rstrip())
        return result

    def _log_exception(
        self, stage: str, op_id: str, correlation: Mapping[str, Any] | None, reason: str
    ) -> None:
        event = {**(correlation or {"state": stage, "operation_id": op_id})}
        event.update(event="exception", reason_code=reason)
        try:
            self.log.append(event)
        except Exception as exc:
            raise ProvenanceIncomplete(f"{op_id}: could not record {reason}") from exc
