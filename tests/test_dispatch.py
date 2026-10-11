"""Tests for the dispatcher's transaction order and failure handling (fake adapters)."""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

import pytest

from agentic_pipeline.dispatch import (
    AdapterResult,
    DispatchBlocked,
    Dispatcher,
    DispatchGrant,
    PreparedCall,
    ProvenanceIncomplete,
    RunPaused,
)
from agentic_pipeline.graph import Edge, Graph, load_yaml
from agentic_pipeline.provenance import JsonlProvenanceLog, verify_provenance_chain
from agentic_pipeline.readiness import Check, Report, Status
from agentic_pipeline.transition import BudgetLedger

ROOT = Path(__file__).resolve().parents[1]
GRAPH = Graph.load(ROOT / "pipeline" / "graph.yaml")
REGISTRY = load_yaml(ROOT / "policy" / "model_registry.yaml")
BUDGETS = load_yaml(ROOT / "policy" / "budgets.yaml")
ISOLATION = (
    "sandbox_enforcement",
    "protected_paths_read_only",
    "network_disabled",
    "proposal_path_layout",
    "session_separation",
    "sealed_input_verification",
    "broker_mediated_inference",
)


def report(*, unready: tuple[str, ...] = (), credential: Status = Status.VERIFIED) -> Report:
    def checks(name: str) -> list[Check]:
        status = Status.UNVERIFIED if name in unready else Status.VERIFIED
        return [Check("subscription_auth", status, "")]

    return Report(
        static_errors=[],
        global_checks=[Check("credential_env_absent", credential, "")],
        providers={p: checks(p) for p in ("claude_pro", "google_ai_pro", "qwen_local")},
        isolation={n: Check(n, Status.VERIFIED, "") for n in ISOLATION},
        registry=REGISTRY,
    )


class FakeAdapter:
    def __init__(self, status: str = "ok", raise_on_invoke: bool = False) -> None:
        self.status = status
        self.raise_on_invoke = raise_on_invoke
        self.grants: list[DispatchGrant] = []

    def prepare(self, grant: DispatchGrant) -> PreparedCall:
        return PreparedCall("fake-model", "1.0", "subscription_tool", grant.operation_id.encode())

    def invoke(self, grant: DispatchGrant, call: PreparedCall) -> AdapterResult:
        self.grants.append(grant)
        if self.raise_on_invoke:
            raise ConnectionError("provider unreachable")
        return AdapterResult(self.status)  # type: ignore[arg-type]


class FailingLog(JsonlProvenanceLog):
    """Fails the Nth append (0-based); earlier appends are real."""

    def __init__(self, directory: Path, fail_at: int) -> None:
        super().__init__(directory, "run-1")
        self.fail_at, self.calls = fail_at, 0

    def append(self, event: Mapping[str, Any]) -> dict[str, Any]:
        self.calls += 1
        if self.calls - 1 == self.fail_at:
            raise OSError("fsync failed")
        return super().append(event)


def allow_all(edge: Edge, ctx: Mapping[str, Any]) -> bool:
    return True


@pytest.fixture
def adapters() -> dict[str, FakeAdapter]:
    return {b: FakeAdapter() for b in ("claude_pro", "google_ai_pro", "qwen_local")}


def make(
    tmp_path: Path,
    adapters: Mapping[str, FakeAdapter],
    *,
    log: JsonlProvenanceLog | None = None,
    reports: Iterator[Report] | None = None,
    guards: Any = allow_all,
    ledger: BudgetLedger | None = None,
) -> Dispatcher:
    reports = reports or iter(lambda: report(), None)
    kwargs = {"guards": guards} if guards is not None else {}
    return Dispatcher(
        graph=GRAPH,
        registry=REGISTRY,
        log=log or JsonlProvenanceLog(tmp_path, "run-1"),
        ledger=ledger or BudgetLedger.from_policy(BUDGETS),
        adapters=adapters,
        preflight=lambda: next(reports),
        **kwargs,
    )


def events(d: Dispatcher) -> list[str]:
    if not d.log.path.exists():
        return []
    return [json.loads(line)["event"] for line in d.log.path.read_text().splitlines()]


def test_a1_happy_path_runs_both_operations_with_role_backends(
    tmp_path: Path, adapters: dict[str, FakeAdapter]
) -> None:
    d = make(tmp_path, adapters)
    outcome = d.dispatch_stage("A2", "blocking_flaw", "A1", {})
    assert set(outcome.results) == {"proposal_a", "proposal_b"} and outcome.provisional
    assert [g.operation_id for g in adapters["claude_pro"].grants] == ["proposal_a"]
    assert [g.operation_id for g in adapters["google_ai_pro"].grants] == ["proposal_b"]
    assert events(d) == ["model_call", "model_result", "model_call", "model_result"]
    assert verify_provenance_chain(d.log.path).ok
    assert d.ledger.consumed == {"design": 1}  # one unit per edge, not per invocation


def test_grant_carries_only_declared_scope(tmp_path: Path, adapters: dict) -> None:
    make(tmp_path, adapters).dispatch_stage("G4", "always", "A1", {})
    grant = adapters["claude_pro"].grants[0]
    assert grant.allowed_reads == (
        "pinned_context_manifest",
        "requirements",
        "applicable_contracts",
        "adr_snapshot",
    )
    assert grant.write_root == "proposal_a" and grant.run_id == "run-1"


def test_ineligible_stage_blocked_before_anything(tmp_path: Path, adapters: dict) -> None:
    d = make(tmp_path, adapters, reports=iter([report(unready=("google_ai_pro",))]))
    with pytest.raises(DispatchBlocked, match="google_ai_pro.subscription_auth"):
        d.dispatch_stage("A2", "blocking_flaw", "A1", {})
    assert events(d) == [] and d.ledger.consumed == {} and d.ledger.reserved == {}


def test_credential_env_blocks_dispatch(tmp_path: Path, adapters: dict) -> None:
    d = make(tmp_path, adapters, reports=iter([report(credential=Status.FAILED)]))
    with pytest.raises(DispatchBlocked, match="credential_env_absent"):
        d.dispatch_stage("G4", "always", "A1", {})


def test_deny_all_guards_by_default(tmp_path: Path, adapters: dict) -> None:
    d = make(tmp_path, adapters, guards=None)
    with pytest.raises(DispatchBlocked, match="not satisfied"):
        d.dispatch_stage("G4", "always", "A1", {})
    assert events(d) == []


def test_missing_adapter_blocks(tmp_path: Path, adapters: dict) -> None:
    del adapters["google_ai_pro"]
    with pytest.raises(DispatchBlocked, match="no adapter"):
        make(tmp_path, adapters).dispatch_stage("G4", "always", "A1", {})


def test_exhausted_budget_blocks(tmp_path: Path, adapters: dict) -> None:
    d = make(tmp_path, adapters, ledger=BudgetLedger({"design": 0}))
    with pytest.raises(DispatchBlocked, match="exhausted"):
        d.dispatch_stage("A2", "blocking_flaw", "A1", {})
    assert events(d) == []


def test_pre_call_log_failure_releases_budget(tmp_path: Path, adapters: dict) -> None:
    d = make(tmp_path, adapters, log=FailingLog(tmp_path, fail_at=0))
    with pytest.raises(DispatchBlocked, match="pre-call"):
        d.dispatch_stage("A2", "blocking_flaw", "A1", {})
    assert d.ledger.consumed == {} and d.ledger.reserved == {"design": 0}
    assert adapters["claude_pro"].grants == []


def test_post_call_log_failure_keeps_budget_consumed(tmp_path: Path, adapters: dict) -> None:
    d = make(tmp_path, adapters, log=FailingLog(tmp_path, fail_at=1))
    with pytest.raises(ProvenanceIncomplete):
        d.dispatch_stage("A2", "blocking_flaw", "A1", {})
    assert d.ledger.consumed == {"design": 1}
    assert verify_provenance_chain(d.log.path).unresolved  # surfaces for reconciliation


def test_adapter_exception_logged_and_pauses(tmp_path: Path, adapters: dict) -> None:
    adapters["claude_pro"].raise_on_invoke = True
    d = make(tmp_path, adapters)
    with pytest.raises(RunPaused, match="adapter failed"):
        d.dispatch_stage("G4", "always", "A1", {})
    assert events(d) == ["model_call", "exception"]
    assert verify_provenance_chain(d.log.path).ok
    assert adapters["google_ai_pro"].grants == []  # second operation never starts


@pytest.mark.parametrize("status", ["quota_exhausted", "auth_failed", "error"])
def test_non_ok_result_pauses_without_fallback(tmp_path: Path, adapters: dict, status: str) -> None:
    adapters["claude_pro"].status = status
    d = make(tmp_path, adapters)
    with pytest.raises(RunPaused, match=status):
        d.dispatch_stage("A2", "no_blocking_flaw", "A3", {})
    assert adapters["google_ai_pro"].grants == []  # no in-flight fallback to the next backend
    assert events(d) == ["model_call", "model_result"]


def test_readiness_lost_between_operations_pauses(tmp_path: Path, adapters: dict) -> None:
    reports = iter([report(), report(unready=("google_ai_pro",))])
    d = make(tmp_path, adapters, reports=reports)
    with pytest.raises(RunPaused, match="readiness lost"):
        d.dispatch_stage("A2", "blocking_flaw", "A1", {})
    assert events(d) == ["model_call", "model_result", "exception"]
    assert adapters["google_ai_pro"].grants == []
    assert d.ledger.consumed == {"design": 1}


def test_ordered_any_of_uses_first_ready_backend(tmp_path: Path, adapters: dict) -> None:
    d = make(tmp_path, adapters, reports=iter(lambda: report(unready=("claude_pro",)), None))
    outcome = d.dispatch_stage("A2", "no_blocking_flaw", "A3", {})
    assert set(outcome.results) == {"synthesis"}
    assert adapters["claude_pro"].grants == [] and len(adapters["google_ai_pro"].grants) == 1
