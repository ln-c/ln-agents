"""Tests for the dispatcher's transaction order and failure handling (fake adapters)."""

from __future__ import annotations

import json
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

import pytest

from agentic_pipeline import dispatch
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
from agentic_pipeline.manifest import (
    build_manifest,
    digest_path,
    load_manifest,
    publish_stage_manifest,
)
from agentic_pipeline.provenance import GENESIS_HASH, JsonlProvenanceLog, verify_provenance_chain
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
UPSTREAM = ("START", "G1", "G4", "DR", "A1", "A2", "A3", "C1", "D1")


def seed(run_root: Path, stages: tuple[str, ...] = UPSTREAM) -> None:
    """Publish manifests with placeholder content for every sealed artifact of `stages`."""
    for stage in stages:
        produced = {
            aid: (run_root / "seed" / aid, True)
            for aid, spec in REGISTRY["artifacts"].items()
            if spec["sealed"] and stage in {p.split(".")[0] for p in spec["producer"]}
        }
        for path, _ in produced.values():
            path.mkdir(parents=True)
            (path / "content.txt").write_text(f"{path.name}\n")
        manifest = build_manifest(
            run_root,
            run_id="run-1",
            stage=stage,
            attempt=f"{stage}-seed",
            artifacts=produced,
            operations={},
            provenance_tip=GENESIS_HASH,
        )
        publish_stage_manifest(run_root, manifest)


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
    enforced_isolation = frozenset(ISOLATION)

    def __init__(self, status: str = "ok", raise_on_invoke: bool = False) -> None:
        self.status = status
        self.raise_on_invoke = raise_on_invoke
        self.grants: list[DispatchGrant] = []
        self.side_effect: Any = None

    def prepare(self, grant: DispatchGrant) -> PreparedCall:
        return PreparedCall("fake-model", "1.0", "subscription_tool", grant.operation_id.encode())

    def invoke(self, grant: DispatchGrant, call: PreparedCall) -> AdapterResult:
        self.grants.append(grant)
        if self.raise_on_invoke:
            raise ConnectionError("provider unreachable")
        (grant.write_path / "output.md").write_text(f"{grant.operation_id}\n")
        if self.side_effect:
            self.side_effect()
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
    upstream: tuple[str, ...] = UPSTREAM,
) -> Dispatcher:
    run_root = tmp_path / "run"
    if not run_root.exists():
        seed(run_root, upstream)
    reports = reports or iter(lambda: report(), None)
    kwargs = {"guards": guards} if guards is not None else {}
    return Dispatcher(
        graph=GRAPH,
        registry=REGISTRY,
        log=log or JsonlProvenanceLog(tmp_path, "run-1"),
        ledger=ledger or BudgetLedger.from_policy(BUDGETS),
        adapters=adapters,
        preflight=lambda: next(reports),
        run_root=run_root,
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
    assert set(outcome.results) == {"proposal_a", "proposal_b"} and not outcome.provisional
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


# ── Sealed inputs and stage manifests ────────────────────────────────────────


def test_manifest_seals_operation_outputs(tmp_path: Path, adapters: dict) -> None:
    d = make(tmp_path, adapters)
    outcome = d.dispatch_stage("G4", "always", "A1", {})
    manifest = load_manifest(d.run_root, "A1", "run-1")
    assert manifest is not None and outcome.manifest == d.run_root / "manifests" / "A1.json"
    assert set(manifest["artifacts"]) == {"proposal_a_sealed", "proposal_b_sealed"}
    grant = adapters["claude_pro"].grants[0]
    entry = manifest["artifacts"]["proposal_a_sealed"]
    assert entry["path"] == f"attempts/{grant.attempt}/design/proposal_a" and entry["sealed"]
    assert entry["digest"] == digest_path(grant.write_path)
    assert manifest["provenance_tip"] == verify_provenance_chain(d.log.path).tip
    assert manifest["operations"]["proposal_b"]["backend"] == "google_ai_pro"


def test_downstream_stage_reads_what_upstream_sealed(tmp_path: Path, adapters: dict) -> None:
    d = make(tmp_path, adapters, upstream=("START", "G1", "G4"))
    d.dispatch_stage("G4", "always", "A1", {})
    outcome = d.dispatch_stage("A1", "always", "A2", {})
    assert not outcome.provisional


def test_unsealed_output_recorded_for_e1(tmp_path: Path, adapters: dict) -> None:
    d = make(tmp_path, adapters)
    d.dispatch_stage("E0", "always", "E1", {})
    manifest = load_manifest(d.run_root, "E1", "run-1")
    assert manifest["artifacts"]["implementation_patch"]["sealed"] is False


def test_missing_upstream_manifest_blocks(tmp_path: Path, adapters: dict) -> None:
    d = make(tmp_path, adapters, upstream=("START", "G1"))
    with pytest.raises(DispatchBlocked, match="pinned_context_manifest: no published manifest"):
        d.dispatch_stage("A2", "blocking_flaw", "A1", {})
    assert events(d) == [] and d.ledger.reserved == {} and d.ledger.consumed == {}


def test_tampered_sealed_input_blocks(tmp_path: Path, adapters: dict) -> None:
    d = make(tmp_path, adapters)
    (d.run_root / "seed" / "requirements" / "content.txt").write_text("edited after sealing\n")
    with pytest.raises(DispatchBlocked, match="requirements: content changed since START"):
        d.dispatch_stage("G4", "always", "A1", {})
    assert not (d.run_root / "attempts").exists()


def test_input_changed_between_operations_pauses(tmp_path: Path, adapters: dict) -> None:
    d = make(tmp_path, adapters)
    target = d.run_root / "seed" / "requirements" / "content.txt"
    adapters["claude_pro"].side_effect = lambda: target.write_text("swapped mid-stage\n")
    with pytest.raises(RunPaused, match="sealed input changed"):
        d.dispatch_stage("G4", "always", "A1", {})
    assert events(d) == ["model_call", "model_result", "exception"]
    assert adapters["google_ai_pro"].grants == []


def test_manifest_publish_failure_leaves_outputs_provisional(
    tmp_path: Path, adapters: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    d = make(tmp_path, adapters)

    def fail(run_root: Path, manifest: Mapping[str, Any]) -> Path:
        raise OSError("disk full")

    monkeypatch.setattr(dispatch, "publish_stage_manifest", fail)
    with pytest.raises(RunPaused, match="outputs provisional"):
        d.dispatch_stage("A2", "blocking_flaw", "A1", {})
    assert load_manifest(d.run_root, "A1", "run-1")["attempt"] == "A1-seed"  # old one untouched
    assert events(d)[-1] == "exception" and d.ledger.consumed == {"design": 1}
    assert verify_provenance_chain(d.log.path).ok


def test_each_attempt_gets_a_fresh_write_root(tmp_path: Path, adapters: dict) -> None:
    d = make(tmp_path, adapters)
    d.dispatch_stage("G4", "always", "A1", {})
    d.dispatch_stage("A2", "blocking_flaw", "A1", {})
    first, second = (g.write_path for g in adapters["claude_pro"].grants)
    assert first != second and first.is_dir() and second.is_dir()


def test_adapter_must_enforce_profile_checks(tmp_path: Path, adapters: dict) -> None:
    adapters["qwen_local"].enforced_isolation = frozenset({"network_disabled"})
    d = make(tmp_path, adapters)
    with pytest.raises(DispatchBlocked, match="qwen_local adapter does not enforce"):
        d.dispatch_stage("E0", "always", "E1", {})
    assert events(d) == [] and not (d.run_root / "attempts").exists()
