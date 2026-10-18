"""Single dispatch bottleneck for model stages.

Order of operations for `dispatch_stage` (failure handling in brackets):

1. Preflight + stage eligibility: static validity, credential denylist,
   backend readiness, isolation profile          [DispatchBlocked, no budget]
2. Adapter present for every selected backend, and
   it enforces every isolation check in the
   operation's profile that the dispatcher does
   not enforce itself                            [DispatchBlocked, no budget]
3. Sealed inputs match their published manifests,
   and their most restrictive data class may go
   to the backend (cloud needs cloud_allowed)    [DispatchBlocked, no budget]
4. Graph edge exists and guard holds             [DispatchBlocked, no budget]
5. Reserve one unit of the edge's budget         [DispatchBlocked if exhausted]
6. Per operation:
   a. re-probe readiness and sealed inputs
      (after the first)                          [RunPaused: readiness_lost,
                                                  sealed_input_changed]
   b. create a fresh attempt write root,
      adapter.prepare (no external call)         [release budget if nothing logged]
   c. append + fsync model_call                  [release budget if first call]
   d. commit the reservation (first call only)   [RunPaused: budget_commit_failed]
   e. adapter.invoke                             [exception event; RunPaused]
   f. output check: an operation with an
      `output_schema` must have written one JSON
      object of that shape, read as the gates read
      it; if not, its model_result is
      `invalid_output`, the output is set aside and
      the operation (only it) is asked again in a
      fresh invocation, up to OUTPUT_RETRIES times,
      re-checking readiness and inputs first      [RunPaused once the re-asks are spent;
                                                  no budget is charged for a re-ask]
   g. append + fsync model_result                [ProvenanceIncomplete]
   h. non-ok result (quota, auth, error)         [RunPaused; no in-flight fallback]
7. Publish manifests/<STAGE>.json atomically     [RunPaused: manifest_publish_failed;
                                                  outputs stay provisional]

Only an outcome with a published manifest is final; the orchestrator must not
advance state on a provisional one.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Literal, Protocol, runtime_checkable

from agentic_pipeline.graph import Graph
from agentic_pipeline.manifest import (
    ATTEMPTS_DIR,
    ManifestError,
    build_manifest,
    publish_stage_manifest,
    resolve_sealed_inputs,
)
from agentic_pipeline.provenance import JsonlProvenanceLog
from agentic_pipeline.readiness import Report, evaluate_stage
from agentic_pipeline.registry import backend_location, operations
from agentic_pipeline.schemas import ValidationError, validate
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
    """Stopped after budget was committed; resuming requires a human."""


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
    role: str
    allowed_reads: tuple[str, ...]
    inputs: Mapping[str, Path]  # verified sealed inputs, artifact_id -> host path
    data_class: str  # most restrictive class of the inputs; outputs inherit it
    write_root: str
    attempt: str
    write_path: Path
    output_schema: str | None = None  # reply must be one JSON object of this shape


@dataclass(frozen=True, slots=True)
class Identity:
    """What an adapter runs: the same three fields every `model_call` event records."""

    model: str
    model_version: str
    access_method: str


@dataclass(frozen=True, slots=True)
class PreparedCall:
    model: str
    model_version: str
    access_method: str
    prompt: bytes

    @property
    def prompt_hash(self) -> str:
        return "sha256:" + hashlib.sha256(self.prompt).hexdigest()


# `invalid_output` is the dispatcher's verdict on an `ok` reply (see output_problems);
# adapters never return it.
type ResultStatus = Literal["ok", "quota_exhausted", "auth_failed", "error", "invalid_output"]


@dataclass(frozen=True, slots=True)
class AdapterResult:
    status: ResultStatus
    detail: str = ""
    resolved_model: str | None = None  # model the provider reports it actually used


# Isolation checks the dispatcher enforces itself; adapters must enforce the rest.
DISPATCHER_ENFORCED = frozenset({"proposal_path_layout", "sealed_input_verification"})
# How many times an operation whose output breaks its schema is asked again, in a fresh
# invocation with the same inputs. A malformed reply is a formatting slip, not a design
# outcome, so a re-ask charges no graph budget; the cap still keeps a model that never gets
# the format right from spending allowance without end.
OUTPUT_RETRIES = 2
REJECTED_DIR = "rejected"  # per attempt: outputs set aside by the output check, never sealed


def output_problems(grant: DispatchGrant) -> list[str]:
    """Why the output is not the one JSON object its schema asks for, read as the gates read it."""
    if grant.output_schema is None:
        return []
    name = f"{grant.operation_id}.json"
    try:
        document = json.loads((grant.write_path / name).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return [f"no {name}: the reply was not one JSON object"]
    except (OSError, ValueError) as exc:
        return [f"{name}: {exc}"]
    try:
        validate(grant.output_schema, document)
    except ValidationError as exc:
        return [f"{name}: schema {grant.output_schema}: {exc.message}"]
    return []


@runtime_checkable
class ExplainsUnavailable(Protocol):
    """Optional on the adapters mapping: why a backend has no adapter."""

    def unavailable(self, backend: str) -> str | None: ...


class BackendAdapter(Protocol):
    # Isolation checks (registry isolation_profiles vocabulary) this adapter enforces.
    enforced_isolation: frozenset[str]

    def identity(self) -> Identity:
        """The model and access path `prepare` reports, without building a prompt."""
        ...

    def prepare(self, grant: DispatchGrant) -> PreparedCall:
        """Resolve model/version and build the prompt. Must not call out."""
        ...

    def invoke(self, grant: DispatchGrant, call: PreparedCall) -> AdapterResult: ...


@dataclass(frozen=True, slots=True)
class StageOutcome:
    stage: str
    results: dict[str, AdapterResult]
    manifest: Path | None = None

    @property
    def provisional(self) -> bool:
        return self.manifest is None


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
    run_root: Path
    data_classes: Mapping[str, Any]  # policy/data_classes.yaml
    guards: GuardEvaluator = deny_all_guards
    new_invocation_id: Callable[[], str] = _new_invocation_id
    _committed: bool = field(default=False, init=False)
    _tip: str = field(default="", init=False)

    def dispatch_stage(
        self,
        src: str,
        guard: str,
        stage: str,
        run_context: Mapping[str, Any],
        human_decision: object | None = None,
    ) -> StageOutcome:
        verdict = evaluate_stage(stage, self.preflight())
        if not verdict.eligible:
            raise DispatchBlocked(f"{stage}: " + "; ".join(verdict.reasons))
        needed = sorted(set(verdict.backends.values()))
        if missing := [b for b in needed if b not in self.adapters]:
            why = []
            for backend in missing:
                reason = (
                    self.adapters.unavailable(backend)
                    if isinstance(self.adapters, ExplainsUnavailable)
                    else None
                )
                why.append(f"{backend}: {reason}" if reason else backend)
            raise DispatchBlocked(f"{stage}: no adapter for {why}")
        spec = self.registry["stage_requirements"][stage]
        ops = [op for _, op in operations({stage: spec})]
        profiles = self.registry["isolation_profiles"]
        for op in ops:
            backend = verdict.backends[op["id"]]
            required = set(profiles[op["isolation_profile"]]) - DISPATCHER_ENFORCED
            if gaps := sorted(required - self.adapters[backend].enforced_isolation):
                raise DispatchBlocked(
                    f"{stage}.{op['id']}: {backend} adapter does not enforce {gaps}"
                )
        checks = [self._inputs(op, verdict.backends[op["id"]])[2] for op in ops]
        if input_errors := [e for errors in checks for e in errors]:
            raise DispatchBlocked(f"{stage}: sealed inputs: " + "; ".join(input_errors))
        try:
            edge = check_transition(
                self.graph, src, guard, stage, self.guards, run_context, human_decision
            )
            reservation = self.ledger.reserve(edge)
        except TransitionDenied as exc:
            raise DispatchBlocked(str(exc)) from exc

        self._committed = False
        attempt = f"{stage}-{uuid.uuid4().hex[:12]}"
        roots = self.registry["workspace_roots"]
        results: dict[str, AdapterResult] = {}
        grants: list[DispatchGrant] = []
        for index, op in enumerate(ops):
            backend = verdict.backends[op["id"]]
            if index > 0:
                self._recheck_readiness(stage, op, backend)
            # Re-verified right before each invocation.
            inputs, data_class = self._recheck_inputs(stage, op, backend, reservation)
            grant = DispatchGrant(
                run_id=self.log.run_id,
                stage=stage,
                operation_id=op["id"],
                backend=backend,
                invocation_id=self.new_invocation_id(),
                role=op["role"],
                allowed_reads=tuple(op["allowed_reads"]),
                inputs=inputs,
                data_class=data_class,
                write_root=op["write_root"],
                attempt=attempt,
                write_path=self.run_root / ATTEMPTS_DIR / attempt / roots[op["write_root"]],
                output_schema=op.get("output_schema"),
            )
            grant, results[op["id"]] = self._invoke_checked(grant, reservation, op)
            grants.append(grant)
        return StageOutcome(stage, results, self._seal(stage, attempt, grants))

    def _recheck_readiness(self, stage: str, op: Mapping[str, Any], backend: str) -> None:
        again = evaluate_stage(stage, self.preflight())
        if not again.eligible or again.backends.get(op["id"]) != backend:
            self._log_exception(stage, op["id"], None, "readiness_lost")
            raise RunPaused(f"{stage}.{op['id']}: readiness lost before invocation")

    def _recheck_inputs(
        self, stage: str, op: Mapping[str, Any], backend: str, reservation: Reservation | None
    ) -> tuple[dict[str, Path], str]:
        inputs, data_class, errors = self._inputs(op, backend)
        if errors:
            if self._committed:
                self._log_exception(stage, op["id"], None, "sealed_input_changed")
                raise RunPaused(f"{stage}.{op['id']}: sealed input changed")
            if reservation is not None:
                reservation.release()
            raise DispatchBlocked(f"{stage}.{op['id']}: sealed input changed")
        return inputs, data_class

    def _invoke_checked(
        self, grant: DispatchGrant, reservation: Reservation | None, op: Mapping[str, Any]
    ) -> tuple[DispatchGrant, AdapterResult]:
        """Invoke; while the output breaks its schema, set it aside and ask again (fresh id)."""
        for retry in range(OUTPUT_RETRIES + 1):
            if retry:
                self._recheck_readiness(grant.stage, op, grant.backend)
                inputs, data_class = self._recheck_inputs(
                    grant.stage, op, grant.backend, reservation
                )
                grant = replace(
                    grant,
                    invocation_id=self.new_invocation_id(),
                    inputs=inputs,
                    data_class=data_class,
                )
            result = self._invoke(grant, reservation)
            if result.status == "ok":
                return grant, result
            rejected = self.run_root / ATTEMPTS_DIR / grant.attempt / REJECTED_DIR
            rejected.mkdir(parents=True, exist_ok=True)
            grant.write_path.rename(rejected / f"{grant.operation_id}-{grant.invocation_id}")
        raise RunPaused(
            f"{grant.operation_id}: invalid_output in {OUTPUT_RETRIES + 1} invocations: "
            f"{result.detail}"
        )

    def _inputs(
        self, op: Mapping[str, Any], backend: str
    ) -> tuple[dict[str, Path], str, list[str]]:
        """Verified input paths, their combined data class, and any blocking errors."""
        found, errors = resolve_sealed_inputs(
            self.run_root, self.log.run_id, self.registry, op["allowed_reads"]
        )
        order = self.data_classes["order"]
        classes = [i.data_class for i in found.values()] + ["unknown"] * bool(errors)
        data_class = max(classes or ["unknown"], key=order.index)
        # Only a provider declared `local` stays on the host; a missing or odd value is cloud.
        location = backend_location(self.registry, backend)
        allowed = self.data_classes["classes"][data_class].get("cloud_allowed") is True
        if location != "local" and not allowed:
            errors.append(f"data class {data_class!r} may not be sent to cloud backend {backend}")
        return {aid: i.path for aid, i in found.items()}, data_class, errors

    def _seal(self, stage: str, attempt: str, grants: list[DispatchGrant]) -> Path:
        catalog = self.registry["artifacts"]
        artifacts: dict[str, tuple[Path, bool]] = {}
        classes: dict[str, str] = {}
        for grant in grants:
            produced = {
                aid: spec["sealed"]
                for aid, spec in catalog.items()
                if f"{stage}.{grant.operation_id}" in spec["producer"]
            }
            if any(produced.values()):  # sealed output supersedes its own scratch
                produced = {aid: True for aid, sealed in produced.items() if sealed}
            artifacts.update({aid: (grant.write_path, sealed) for aid, sealed in produced.items()})
            classes.update(dict.fromkeys(produced, grant.data_class))
        try:
            manifest = build_manifest(
                self.run_root,
                run_id=self.log.run_id,
                stage=stage,
                attempt=attempt,
                artifacts=artifacts,
                operations={
                    g.operation_id: {
                        "backend": g.backend,
                        "invocation_id": g.invocation_id,
                        "status": "ok",
                    }
                    for g in grants
                },
                provenance_tip=self._tip,
                data_classes=classes,
            )
            return publish_stage_manifest(self.run_root, manifest)
        except (ManifestError, OSError) as exc:
            self._log_exception(stage, "manifest", {"state": stage}, "manifest_publish_failed")
            raise RunPaused(f"{stage}: manifest not published, outputs provisional: {exc}") from exc

    def _invoke(self, grant: DispatchGrant, reservation: Reservation | None) -> AdapterResult:
        adapter = self.adapters[grant.backend]
        correlation = {
            "state": grant.stage,
            "backend": grant.backend,
            "operation_id": grant.operation_id,
            "invocation_id": grant.invocation_id,
        }
        try:
            grant.write_path.mkdir(parents=True, exist_ok=False)
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
        if result.status == "ok" and (problems := output_problems(grant)):
            result = AdapterResult("invalid_output", "; ".join(problems), result.resolved_model)

        try:
            record = {**correlation, "event": "model_result", "reason_code": result.status}
            if result.resolved_model:
                record["model"] = result.resolved_model
            self._tip = self.log.append(record)["event_hash"]
        except Exception as exc:
            raise ProvenanceIncomplete(f"{grant.operation_id}: result not recorded") from exc
        if result.status not in ("ok", "invalid_output"):
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
