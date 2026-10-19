"""Tests for the dispatcher's transaction order and failure handling (fake adapters)."""

from __future__ import annotations

import copy
import hashlib
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
DATA_CLASSES = load_yaml(ROOT / "policy" / "data_classes.yaml")
# The backends the registry binds today, so these tests follow a rebinding.
DESIGN = REGISTRY["agents"]["design"]
A = DESIGN["proposal_a"]["primary"]  # Design A: proposal A, and A's critique of B
B = DESIGN["proposal_b"]["primary"]  # Design B
SYNTHESIZER = DESIGN["synthesis"]["model_assistance"][0]
REVIEWERS = DESIGN["synthesis_review"]["model_assistance"]  # in preference order
BINDABLE = tuple(name for name, model in REGISTRY["models"].items() if model["capabilities"])
# A recorded reply of each output schema, which a fake writes so the output check passes.
RECORDED = ROOT / "tests" / "fixtures" / "deploy-design"
SCHEMA_REPLIES = {
    "proposal": RECORDED / "A1" / "proposal_a.json",
    "critique": RECORDED / "A2" / "critique_a_by_b.json",
}
ISOLATION = (
    "sandbox_enforcement",
    "protected_paths_read_only",
    "network_disabled",
    "proposal_path_layout",
    "session_separation",
    "sealed_input_verification",
    "broker_mediated_inference",
)
UPSTREAM = ("START", "G", "DR", "A1", "A2", "A3", "C1", "D1")


def seed(
    run_root: Path, stages: tuple[str, ...] = UPSTREAM, data_class: str | None = "internal"
) -> None:
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
            data_classes=dict.fromkeys(produced, data_class) if data_class else None,
        )
        publish_stage_manifest(run_root, manifest)


def report(*, unready: tuple[str, ...] = (), credential: Status = Status.VERIFIED) -> Report:
    models = REGISTRY["models"]
    down = {
        models[m]["provider"] for m in unready
    }  # `unready` names models; probes are per provider

    def checks(name: str) -> list[Check]:
        return [Check("executable", Status.UNVERIFIED if name in down else Status.VERIFIED, "")]

    return Report(
        static_errors=[],
        global_checks=[Check("credential_env_absent", credential, "")],
        providers={
            p: checks(p)
            for p in ("claude_pro", "google_ai_pro", "llamacpp_local", "llamacpp_decision")
        },
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
        self.resolved_model: str | None = None
        self.malformed: list[bytes | None] = []  # replies before a good one; None: no JSON

    def prepare(self, grant: DispatchGrant) -> PreparedCall:
        return PreparedCall("fake-model", "1.0", "subscription_tool", grant.operation_id.encode())

    def invoke(self, grant: DispatchGrant, call: PreparedCall) -> AdapterResult:
        self.grants.append(grant)
        if self.raise_on_invoke:
            raise ConnectionError("provider unreachable")
        (grant.write_path / "output.md").write_text(f"{grant.operation_id}\n")
        if grant.output_schema is not None:
            good = SCHEMA_REPLIES[grant.output_schema].read_bytes()
            reply = self.malformed.pop(0) if self.malformed else good
            if reply is not None:
                (grant.write_path / f"{grant.operation_id}.json").write_bytes(reply)
        if self.side_effect:
            self.side_effect()
        return AdapterResult(self.status, resolved_model=self.resolved_model)  # type: ignore[arg-type]


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
    return {b: FakeAdapter() for b in BINDABLE}


def make(
    tmp_path: Path,
    adapters: Mapping[str, FakeAdapter],
    *,
    log: JsonlProvenanceLog | None = None,
    reports: Iterator[Report] | None = None,
    guards: Any = allow_all,
    ledger: BudgetLedger | None = None,
    upstream: tuple[str, ...] = UPSTREAM,
    data_class: str | None = "internal",
) -> Dispatcher:
    run_root = tmp_path / "run"
    if not run_root.exists():
        seed(run_root, upstream, data_class)
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
        data_classes=DATA_CLASSES,
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
    outcome = d.dispatch_stage("A4", "proposal_invalid", "A1", {})
    assert set(outcome.results) == {"proposal_a", "proposal_b"} and not outcome.provisional
    assert [g.operation_id for g in adapters[A].grants] == ["proposal_a"]
    assert [g.operation_id for g in adapters[B].grants] == ["proposal_b"]
    assert events(d) == ["model_call", "model_result", "model_call", "model_result"]
    assert verify_provenance_chain(d.log.path).ok
    assert d.ledger.consumed == {"design": 1}  # one unit per edge, not per invocation


def test_grant_carries_only_declared_scope(tmp_path: Path, adapters: dict) -> None:
    make(tmp_path, adapters).dispatch_stage("G", "always", "A1", {})
    grant = adapters[A].grants[0]
    assert grant.allowed_reads == (
        "pinned_context_manifest",
        "requirements",
        "applicable_contracts",
    )
    assert grant.write_root == "proposal_a" and grant.run_id == "run-1"
    assert grant.output_schema == "proposal"  # from the registry, not the adapter


def test_grant_carries_the_agents_frozen_spec(tmp_path: Path, adapters: dict) -> None:
    d = make(tmp_path, adapters)
    design, build = "agents/pipeline-design-agent.md", "agents/pipeline-build-agent.md"
    d.specs = {design: "# Design\n", build: "# Build\n"}
    d.dispatch_stage("G", "always", "A1", {})
    for grants in (adapters[A].grants, adapters[B].grants):  # both sides serve the design agent
        spec = grants[0].agent_spec
        assert spec is not None and spec.path == design and spec.text == "# Design\n"
        assert spec.digest == "sha256:" + hashlib.sha256(b"# Design\n").hexdigest()


def test_a_run_that_froze_no_specs_gets_none(tmp_path: Path, adapters: dict) -> None:
    make(tmp_path, adapters).dispatch_stage("G", "always", "A1", {})
    assert adapters[A].grants[0].agent_spec is None


def test_ineligible_stage_blocked_before_anything(tmp_path: Path, adapters: dict) -> None:
    d = make(tmp_path, adapters, reports=iter([report(unready=(B,))]))
    with pytest.raises(DispatchBlocked, match=f"{B}.executable"):
        d.dispatch_stage("A4", "proposal_invalid", "A1", {})
    assert events(d) == [] and d.ledger.consumed == {} and d.ledger.reserved == {}


def test_credential_env_blocks_dispatch(tmp_path: Path, adapters: dict) -> None:
    d = make(tmp_path, adapters, reports=iter([report(credential=Status.FAILED)]))
    with pytest.raises(DispatchBlocked, match="credential_env_absent"):
        d.dispatch_stage("G", "always", "A1", {})


def test_deny_all_guards_by_default(tmp_path: Path, adapters: dict) -> None:
    d = make(tmp_path, adapters, guards=None)
    with pytest.raises(DispatchBlocked, match="not satisfied"):
        d.dispatch_stage("G", "always", "A1", {})
    assert events(d) == []


def test_missing_adapter_blocks(tmp_path: Path, adapters: dict) -> None:
    del adapters[B]
    with pytest.raises(DispatchBlocked, match="no adapter"):
        make(tmp_path, adapters).dispatch_stage("G", "always", "A1", {})


def test_exhausted_budget_blocks(tmp_path: Path, adapters: dict) -> None:
    d = make(tmp_path, adapters, ledger=BudgetLedger({"design": 0}))
    with pytest.raises(DispatchBlocked, match="exhausted"):
        d.dispatch_stage("A4", "proposal_invalid", "A1", {})
    assert events(d) == []


def test_pre_call_log_failure_releases_budget(tmp_path: Path, adapters: dict) -> None:
    d = make(tmp_path, adapters, log=FailingLog(tmp_path, fail_at=0))
    with pytest.raises(DispatchBlocked, match="pre-call"):
        d.dispatch_stage("A4", "proposal_invalid", "A1", {})
    assert d.ledger.consumed == {} and d.ledger.reserved == {"design": 0}
    assert adapters[A].grants == []


def test_post_call_log_failure_keeps_budget_consumed(tmp_path: Path, adapters: dict) -> None:
    d = make(tmp_path, adapters, log=FailingLog(tmp_path, fail_at=1))
    with pytest.raises(ProvenanceIncomplete):
        d.dispatch_stage("A4", "proposal_invalid", "A1", {})
    assert d.ledger.consumed == {"design": 1}
    assert verify_provenance_chain(d.log.path).unresolved  # surfaces for reconciliation


def test_adapter_exception_logged_and_pauses(tmp_path: Path, adapters: dict) -> None:
    adapters[A].raise_on_invoke = True
    d = make(tmp_path, adapters)
    with pytest.raises(RunPaused, match="adapter failed"):
        d.dispatch_stage("G", "always", "A1", {})
    assert events(d) == ["model_call", "exception"]
    assert verify_provenance_chain(d.log.path).ok
    assert adapters[B].grants == []  # second operation never starts


@pytest.mark.parametrize("status", ["quota_exhausted", "auth_failed", "error"])
def test_non_ok_result_pauses_without_fallback(tmp_path: Path, adapters: dict, status: str) -> None:
    first, then = REVIEWERS[:2]
    adapters[first].status = status
    d = make(tmp_path, adapters)
    with pytest.raises(RunPaused, match=status):
        d.dispatch_stage("A4", "all_required_valid", "A3R", {})
    assert adapters[then].grants == []  # no in-flight fallback to the next backend
    assert events(d) == ["model_call", "model_result"]


def test_readiness_lost_between_operations_pauses(tmp_path: Path, adapters: dict) -> None:
    reports = iter([report(), report(unready=(B,))])
    d = make(tmp_path, adapters, reports=reports)
    with pytest.raises(RunPaused, match="readiness lost"):
        d.dispatch_stage("A4", "proposal_invalid", "A1", {})
    assert events(d) == ["model_call", "model_result", "exception"]
    assert adapters[B].grants == []
    assert d.ledger.consumed == {"design": 1}


def test_ordered_any_of_uses_first_ready_backend(tmp_path: Path, adapters: dict) -> None:
    first, then = REVIEWERS[:2]
    d = make(tmp_path, adapters, reports=iter(lambda: report(unready=(first,)), None))
    outcome = d.dispatch_stage("A4", "all_required_valid", "A3R", {})
    assert set(outcome.results) == {"synthesis_review"}
    assert adapters[first].grants == [] and len(adapters[then].grants) == 1


# ── Sealed inputs and stage manifests ────────────────────────────────────────


def test_manifest_seals_operation_outputs(tmp_path: Path, adapters: dict) -> None:
    d = make(tmp_path, adapters)
    outcome = d.dispatch_stage("G", "always", "A1", {})
    manifest = load_manifest(d.run_root, "A1", "run-1")
    assert manifest is not None and outcome.manifest == d.run_root / "manifests" / "A1.json"
    assert set(manifest["artifacts"]) == {"proposal_a_sealed", "proposal_b_sealed"}
    grant = adapters[A].grants[0]
    entry = manifest["artifacts"]["proposal_a_sealed"]
    assert entry["path"] == f"attempts/{grant.attempt}/design/proposal_a" and entry["sealed"]
    assert entry["digest"] == digest_path(grant.write_path)
    assert manifest["provenance_tip"] == verify_provenance_chain(d.log.path).tip
    assert manifest["operations"]["proposal_b"]["backend"] == B


def test_downstream_stage_reads_what_upstream_sealed(tmp_path: Path, adapters: dict) -> None:
    d = make(tmp_path, adapters, upstream=("START", "G"))
    d.dispatch_stage("G", "always", "A1", {})
    outcome = d.dispatch_stage("A1", "always", "A2", {})
    assert not outcome.provisional


def test_unsealed_output_recorded_for_e1(tmp_path: Path, adapters: dict) -> None:
    d = make(tmp_path, adapters)
    d.dispatch_stage("E0", "always", "E1", {})
    manifest = load_manifest(d.run_root, "E1", "run-1")
    assert manifest["artifacts"]["implementation_patch"]["sealed"] is False


def test_missing_upstream_manifest_blocks(tmp_path: Path, adapters: dict) -> None:
    d = make(tmp_path, adapters, upstream=("START",))
    with pytest.raises(DispatchBlocked, match="pinned_context_manifest: no published manifest"):
        d.dispatch_stage("A4", "proposal_invalid", "A1", {})
    assert events(d) == [] and d.ledger.reserved == {} and d.ledger.consumed == {}


def test_tampered_sealed_input_blocks(tmp_path: Path, adapters: dict) -> None:
    d = make(tmp_path, adapters)
    (d.run_root / "seed" / "requirements" / "content.txt").write_text("edited after sealing\n")
    with pytest.raises(DispatchBlocked, match="requirements: content changed since START"):
        d.dispatch_stage("G", "always", "A1", {})
    assert not (d.run_root / "attempts").exists()


def test_input_changed_between_operations_pauses(tmp_path: Path, adapters: dict) -> None:
    d = make(tmp_path, adapters)
    target = d.run_root / "seed" / "requirements" / "content.txt"
    adapters[A].side_effect = lambda: target.write_text("swapped mid-stage\n")
    with pytest.raises(RunPaused, match="sealed input changed"):
        d.dispatch_stage("G", "always", "A1", {})
    assert events(d) == ["model_call", "model_result", "exception"]
    assert adapters[B].grants == []


def test_manifest_publish_failure_leaves_outputs_provisional(
    tmp_path: Path, adapters: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    d = make(tmp_path, adapters)

    def fail(run_root: Path, manifest: Mapping[str, Any]) -> Path:
        raise OSError("disk full")

    monkeypatch.setattr(dispatch, "publish_stage_manifest", fail)
    with pytest.raises(RunPaused, match="outputs provisional"):
        d.dispatch_stage("A4", "proposal_invalid", "A1", {})
    assert load_manifest(d.run_root, "A1", "run-1")["attempt"] == "A1-seed"  # old one untouched
    assert events(d)[-1] == "exception" and d.ledger.consumed == {"design": 1}
    assert verify_provenance_chain(d.log.path).ok


def test_each_attempt_gets_a_fresh_write_root(tmp_path: Path, adapters: dict) -> None:
    d = make(tmp_path, adapters)
    d.dispatch_stage("G", "always", "A1", {})
    d.dispatch_stage("A4", "proposal_invalid", "A1", {})
    first, second = (g.write_path for g in adapters[A].grants)
    assert first != second and first.is_dir() and second.is_dir()


def test_adapter_must_enforce_profile_checks(tmp_path: Path, adapters: dict) -> None:
    adapters["qwen3_5_9b"].enforced_isolation = frozenset({"network_disabled"})
    d = make(tmp_path, adapters)
    with pytest.raises(DispatchBlocked, match="qwen3_5_9b adapter does not enforce"):
        d.dispatch_stage("E0", "always", "E1", {})
    assert events(d) == [] and not (d.run_root / "attempts").exists()


def test_grant_carries_verified_input_paths(tmp_path: Path, adapters: dict) -> None:
    d = make(tmp_path, adapters)
    d.dispatch_stage("E0", "always", "E1", {})
    g = adapters["qwen3_5_9b"].grants[0]
    assert g.role == "patch_author"
    assert set(g.inputs) == {
        "pinned_context_manifest",
        "pinned_source_tree",
        "contracts_sealed",
        "generated_tests_sealed",
        "protected_tests",
    }
    assert g.inputs["contracts_sealed"] == (d.run_root / "seed" / "contracts_sealed").resolve()


# ── Data classes ─────────────────────────────────────────────────────────────


@pytest.mark.parametrize("data_class", [None, "unknown", "confidential", "restricted"])
def test_cloud_backend_blocked_for_non_cloud_data(
    tmp_path: Path, adapters: dict, data_class: str | None
) -> None:
    d = make(tmp_path, adapters, data_class=data_class)
    expected = data_class or "unknown"  # unclassified inputs count as unknown
    with pytest.raises(DispatchBlocked, match=f"data class '{expected}' may not be sent"):
        d.dispatch_stage("G", "always", "A1", {})
    assert events(d) == [] and d.ledger.reserved == {} and adapters[A].grants == []


def test_a_provider_declared_cloud_is_blocked_whatever_its_access_mode(
    tmp_path: Path, adapters: dict
) -> None:
    registry = copy.deepcopy(REGISTRY)
    registry["providers"]["llamacpp_local"]["access_mode"] = "a_new_mode"
    registry["providers"]["llamacpp_local"]["location"] = "cloud"
    d = make(tmp_path, adapters, data_class="confidential")
    d.registry = registry
    with pytest.raises(DispatchBlocked, match="may not be sent to cloud backend qwen3_5_9b"):
        d.dispatch_stage("E0", "always", "E1", {})
    assert adapters["qwen3_5_9b"].grants == []


@pytest.mark.parametrize("location", [None, "elsewhere"])
def test_a_provider_without_a_declared_local_location_is_treated_as_cloud(
    tmp_path: Path, adapters: dict, location: str | None
) -> None:
    registry = copy.deepcopy(REGISTRY)
    if location is None:
        del registry["providers"]["llamacpp_local"]["location"]
    else:
        registry["providers"]["llamacpp_local"]["location"] = location
    d = make(tmp_path, adapters, data_class="confidential")
    d.registry = registry
    with pytest.raises(DispatchBlocked, match="may not be sent to cloud backend qwen3_5_9b"):
        d.dispatch_stage("E0", "always", "E1", {})


def test_local_backend_accepts_confidential_data(tmp_path: Path, adapters: dict) -> None:
    d = make(tmp_path, adapters, data_class="confidential")
    d.dispatch_stage("E0", "always", "E1", {})
    assert adapters["qwen3_5_9b"].grants[0].data_class == "confidential"


def test_outputs_inherit_most_restrictive_input_class(tmp_path: Path, adapters: dict) -> None:
    d = make(tmp_path, adapters, data_class="public")
    manifest_file = d.run_root / "manifests" / "G.json"
    doc = json.loads(manifest_file.read_text())
    doc["artifacts"]["protected_tests"]["data_class"] = "restricted"
    manifest_file.write_text(json.dumps(doc))
    d.dispatch_stage("E0", "always", "E1", {})
    manifest = load_manifest(d.run_root, "E1", "run-1")
    assert manifest["artifacts"]["implementation_patch"]["data_class"] == "restricted"


def test_resolved_model_recorded_in_provenance(tmp_path: Path, adapters: dict) -> None:
    adapters[SYNTHESIZER].resolved_model = "claude-opus-5-5"
    d = make(tmp_path, adapters)
    d.dispatch_stage("A2J", "always", "A3", {})
    results = [json.loads(line) for line in d.log.path.read_text().splitlines()]
    assert [r.get("model") for r in results if r["event"] == "model_result"] == ["claude-opus-5-5"]


def test_missing_adapter_reason_is_surfaced(tmp_path: Path, adapters: dict) -> None:
    class Explained(dict):
        def unavailable(self, name: str) -> str | None:
            return "LookupError: not signed in" if name == B else None

    explained = Explained({k: v for k, v in adapters.items() if k != B})
    with pytest.raises(DispatchBlocked, match=f"{B}: LookupError: not signed in"):
        make(tmp_path, explained).dispatch_stage("G", "always", "A1", {})


def reasons(d: Dispatcher) -> list[str]:
    records = [json.loads(line) for line in d.log.path.read_text().splitlines()]
    return [r["reason_code"] for r in records if r["event"] == "model_result"]


@pytest.mark.parametrize(
    "reply",
    [None, b'{"findings": [}', b'{"id": "not a critique"}'],
    ids=["no-json", "broken-json", "wrong-shape"],
)
def test_a_malformed_reply_is_asked_again_and_only_the_good_one_sealed(
    tmp_path: Path, adapters: dict, reply: bytes | None
) -> None:
    adapters[B].malformed = [reply]
    d = make(tmp_path, adapters)
    outcome = d.dispatch_stage("A1", "always", "A2", {})
    assert not outcome.provisional and outcome.results["critique_a_by_b"].status == "ok"
    assert [g.operation_id for g in adapters[A].grants] == ["critique_b_by_a"]  # not re-asked
    bad, good = adapters[B].grants
    assert bad.invocation_id != good.invocation_id and bad.write_path == good.write_path
    assert reasons(d) == ["ok", "invalid_output", "ok"]
    assert verify_provenance_chain(d.log.path).ok
    manifest = load_manifest(d.run_root, "A2", "run-1")
    assert manifest["operations"]["critique_a_by_b"]["invocation_id"] == good.invocation_id
    assert manifest["artifacts"]["critique_a_by_b_sealed"]["digest"] == digest_path(good.write_path)
    rejected = d.run_root / "attempts" / good.attempt / "rejected"
    assert [p.name for p in rejected.iterdir()] == [f"critique_a_by_b-{bad.invocation_id}"]


def test_a_re_ask_charges_no_budget(tmp_path: Path, adapters: dict) -> None:
    adapters[A].malformed = [None, None]
    d = make(tmp_path, adapters)
    d.dispatch_stage("A4", "proposal_invalid", "A1", {})
    assert len(adapters[A].grants) == 3 and d.ledger.consumed == {"design": 1}


def test_re_asks_are_capped_then_the_run_pauses(tmp_path: Path, adapters: dict) -> None:
    adapters[SYNTHESIZER].malformed = [None] * (dispatch.OUTPUT_RETRIES + 1)
    d = make(tmp_path, adapters)
    with pytest.raises(RunPaused, match=f"invalid_output in {dispatch.OUTPUT_RETRIES + 1} "):
        d.dispatch_stage("A2J", "always", "A3", {})
    assert len(adapters[SYNTHESIZER].grants) == dispatch.OUTPUT_RETRIES + 1
    assert reasons(d) == ["invalid_output"] * (dispatch.OUTPUT_RETRIES + 1)
    assert load_manifest(d.run_root, "A3", "run-1")["attempt"] == "A3-seed"  # nothing sealed
    assert not adapters[SYNTHESIZER].grants[-1].write_path.exists()  # all set aside
    assert verify_provenance_chain(d.log.path).ok


def test_a_re_ask_rechecks_the_sealed_inputs(tmp_path: Path, adapters: dict) -> None:
    d = make(tmp_path, adapters)
    target = d.run_root / "seed" / "proposal_a_sealed" / "content.txt"
    adapters[B].malformed = [None]
    adapters[B].side_effect = lambda: target.write_text("swapped before the re-ask\n")
    with pytest.raises(RunPaused, match="sealed input changed"):
        d.dispatch_stage("A1", "always", "A2", {})
    assert len(adapters[B].grants) == 1
    assert events(d)[-1] == "exception"


def test_an_operation_without_an_output_schema_is_not_checked(
    tmp_path: Path, adapters: dict
) -> None:
    d = make(tmp_path, adapters)
    d.dispatch_stage("E0", "always", "E1", {})  # writes no JSON at all
    assert reasons(d) == ["ok"]
