"""Tests for model registry invariants (R1-R18)."""

from __future__ import annotations

import copy
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from agentic_pipeline.graph import Graph, load_yaml
from agentic_pipeline.registry import (
    op_backends,
    operations,
    resolve_family,
    validate_registry,
)

ROOT = Path(__file__).resolve().parents[1]
REGISTRY = load_yaml(ROOT / "policy" / "model_registry.yaml")
GRAPH = Graph.load(ROOT / "pipeline" / "graph.yaml")

type Registry = dict[str, Any]


def op(reg: Registry, stage: str, op_id: str) -> dict[str, Any]:
    return next(o for o in reg["stage_requirements"][stage]["operations"] if o["id"] == op_id)


def _swap_proposal_a_backend(r: Registry) -> None:
    r["agents"]["design"]["proposal_a"]["primary"] = "google_ai_pro"


def _self_critique(r: Registry) -> None:
    critic = op(r, "A2", "critique_b_by_a")
    critic["target_artifact"] = "proposal_a_sealed"
    critic["allowed_reads"] = ["pinned_context_manifest", "proposal_a_sealed"]


def _unknown_output_schema(r: Registry) -> None:
    op(r, "A1", "proposal_a")["output_schema"] = "ghost"


def _read_other_scratch(r: Registry) -> None:
    op(r, "A1", "proposal_a")["allowed_reads"].append("proposal_b_scratch")
    op(r, "A1", "proposal_a")["denied_reads"].remove("proposal_b_scratch")


def _read_sibling_critique(r: Registry) -> None:
    critic = op(r, "A2", "critique_a_by_b")
    critic["allowed_reads"].append("critique_b_by_a_sealed")
    critic["denied_reads"].remove("critique_b_by_a_sealed")


def _shared_write_root(r: Registry) -> None:
    op(r, "A1", "proposal_b")["write_root"] = "proposal_a"


def _nested_write_root(r: Registry) -> None:
    r["workspace_roots"]["synthesis"] = "design/proposal_a/synthesis"


def _drop_bench(r: Registry) -> None:
    del r["stage_requirements"]["BENCH"]


def _map_code_state(r: Registry) -> None:
    r["stage_requirements"]["G1"] = {"mode": "disabled", "reason": "not a model state"}


def _escape_spec(r: Registry) -> None:
    r["agents"]["build"]["spec"] = "../outside.md"


def _missing_spec(r: Registry) -> None:
    r["agents"]["build"]["spec"] = "agents/does-not-exist.md"


def _drop_denylist_entry(r: Registry) -> None:
    r["policy"]["prohibited_credential_env_vars"].remove("ANTHROPIC_API_KEY")


def _unknown_provider(r: Registry) -> None:
    r["agents"]["release"]["explanation_fallback"] = "gpt_paid"


def _unknown_agent(r: Registry) -> None:
    op(r, "E1", "implementation_patch")["agent"] = "build.missing"


def _non_model_backend(r: Registry) -> None:
    r["agents"]["design"]["synthesis"]["model_assistance"].append("deterministic_rules")


def _no_backend(r: Registry) -> None:
    r["agents"]["build"]["primary"] = "deterministic_rules"  # a non-model primary is skipped


def _stage_names_a_backend(r: Registry) -> None:
    r["stage_requirements"]["E1"]["backends"] = ["claude_pro"]


def _build_on_the_classifier(r: Registry) -> None:
    r["agents"]["build"]["primary"] = "clef_flash_free"


def _no_capabilities_declared(r: Registry) -> None:
    del r["providers"]["qwen_local"]["capabilities"]


def _operation_requires_nothing(r: Registry) -> None:
    op(r, "E1", "implementation_patch")["requires"] = []


def _unknown_capability(r: Registry) -> None:
    op(r, "E1", "implementation_patch")["requires"] = ["telepathy"]


def _unknown_artifact(r: Registry) -> None:
    op(r, "D1", "test_generation")["allowed_reads"].append("secret_store")


def _allowed_and_denied(r: Registry) -> None:
    op(r, "E1", "implementation_patch")["denied_reads"].append("protected_tests")


def _unsealed_target(r: Registry) -> None:
    r["artifacts"]["proposal_b_sealed"]["sealed"] = False


def _unreadable_target(r: Registry) -> None:
    op(r, "A2", "critique_b_by_a")["allowed_reads"].remove("proposal_b_sealed")


def _unknown_profile(r: Registry) -> None:
    op(r, "A3", "synthesis")["isolation_profile"] = "trust_me"


def _unknown_producer_state(r: Registry) -> None:
    r["artifacts"]["requirements"]["producer"] = ["NOWHERE"]


def _unknown_producer_op(r: Registry) -> None:
    r["artifacts"]["synthesis_sealed"]["producer"] = ["A3.missing"]


def _google_serves_claude(r: Registry) -> None:
    r["providers"]["google_ai_pro"]["model_id"] = "claude-sonnet-x"


def _google_serves_unknown_model(r: Registry) -> None:
    r["providers"]["google_ai_pro"]["model_id"] = "mystery-1"


def _two_provider_ids_one_family(r: Registry) -> None:
    r["providers"]["google_ai_pro"].pop("family_by_model_id")
    r["providers"]["google_ai_pro"]["family"] = "claude"


def _both_family_forms(r: Registry) -> None:
    r["providers"]["claude_pro"]["family_by_model_id"] = [{"pattern": "^c", "family": "claude"}]


def _no_family(r: Registry) -> None:
    del r["providers"]["qwen_local"]["family"]


def _bad_family_pattern(r: Registry) -> None:
    r["providers"]["google_ai_pro"]["family_by_model_id"].insert(0, {"pattern": "(", "family": "x"})


def _bad_pattern_after_a_match(r: Registry) -> None:
    r["providers"]["google_ai_pro"]["family_by_model_id"].append({"pattern": "(", "family": "x"})


def _cloud_declared_local(r: Registry) -> None:
    r["providers"]["claude_pro"]["location"] = "local"


def _local_declared_cloud(r: Registry) -> None:
    r["providers"]["qwen_local"]["location"] = "cloud"


def _unknown_access_mode(r: Registry) -> None:
    r["providers"]["qwen_local"]["access_mode"] = "carrier_pigeon"


def _no_location(r: Registry) -> None:
    del r["providers"]["claude_pro"]["location"]


CASES: list[tuple[str, Callable[[Registry], None], str]] = [
    ("schema: denylist floor", _drop_denylist_entry, "registry R1"),
    ("path escapes root", _escape_spec, "registry R2"),
    ("path missing", _missing_spec, "registry R2"),
    ("unknown provider", _unknown_provider, "registry R3"),
    ("model state unmapped", _drop_bench, "registry R4 BENCH"),
    ("code state mapped", _map_code_state, "registry R4 G1"),
    ("unknown agent", _unknown_agent, "registry R5 E1.implementation_patch"),
    ("non-model backend", _non_model_backend, "registry R6 A3.synthesis"),
    ("no backend resolved", _no_backend, "registry R6 C1.module_decomposition"),
    ("stage names a backend", _stage_names_a_backend, "registry R1"),
    ("build on the classifier", _build_on_the_classifier, "registry R18 E1.implementation_patch"),
    ("no capabilities declared", _no_capabilities_declared, "registry R18 C1.module_decomposition"),
    ("operation requires nothing", _operation_requires_nothing, "registry R1"),
    ("unknown capability", _unknown_capability, "registry R1"),
    ("unknown artifact", _unknown_artifact, "registry R7 D1.test_generation"),
    ("allowed and denied", _allowed_and_denied, "registry R8 E1.implementation_patch"),
    ("unsealed target", _unsealed_target, "registry R9 A2.critique_b_by_a"),
    ("unreadable target", _unreadable_target, "registry R9 A2.critique_b_by_a"),
    ("reads other scratch", _read_other_scratch, "registry R10 A1.proposal_a"),
    ("reads sibling scratch in stage", _read_other_scratch, "registry R11 A1.proposal_a"),
    ("reads sibling critique", _read_sibling_critique, "registry R11 A2.critique_a_by_b"),
    ("shared write root", _shared_write_root, "registry R12 A1.proposal_b"),
    ("nested write root", _nested_write_root, "registry R12: workspace roots"),
    ("unknown profile", _unknown_profile, "registry R13 A3.synthesis"),
    ("unknown producer state", _unknown_producer_state, "registry R13 artifact requirements"),
    ("unknown producer op", _unknown_producer_op, "registry R13 artifact synthesis_sealed"),
    ("unknown output schema", _unknown_output_schema, "registry R15 A1.proposal_a"),
    ("same backend for A and B", _swap_proposal_a_backend, "registry R14 A1.proposal_b"),
    ("self-critique", _self_critique, "registry R14 A2.critique_b_by_a"),
    ("same family via another provider", _google_serves_claude, "registry R14 A1.proposal_b"),
    ("same family, fixed", _two_provider_ids_one_family, "registry R14 A1.proposal_b"),
    ("schema: location required", _no_location, "registry R1"),
    ("cloud mode declared local", _cloud_declared_local, "registry R16 providers.claude_pro"),
    ("local mode declared cloud", _local_declared_cloud, "registry R16 providers.qwen_local"),
    ("unknown access mode", _unknown_access_mode, "registry R16 providers.qwen_local"),
    ("model id matches no family", _google_serves_unknown_model, "registry R17 providers.google"),
    ("both family forms", _both_family_forms, "registry R17 providers.claude_pro"),
    ("no family", _no_family, "registry R17 providers.qwen_local"),
    ("bad family pattern", _bad_family_pattern, "registry R17 providers.google_ai_pro"),
    (
        "bad pattern after a match",
        _bad_pattern_after_a_match,
        "registry R17 providers.google_ai_pro",
    ),
]


def test_repo_registry_valid() -> None:
    errors = validate_registry(REGISTRY, GRAPH, ROOT)
    assert not errors, errors


@pytest.mark.parametrize(("mutate", "expected"), [c[1:] for c in CASES], ids=[c[0] for c in CASES])
def test_registry_violation_detected(mutate: Callable[[Registry], None], expected: str) -> None:
    registry = copy.deepcopy(REGISTRY)
    mutate(registry)
    errors = validate_registry(registry, GRAPH, ROOT)
    assert any(err.startswith(expected) for err in errors), errors


def test_schema_failure_short_circuits() -> None:
    registry = copy.deepcopy(REGISTRY)
    _drop_denylist_entry(registry)
    _drop_bench(registry)
    errors = validate_registry(registry, GRAPH, ROOT)
    assert len(errors) == 1 and errors[0].startswith("registry R1"), errors


def test_an_unresolved_family_is_reported_once_not_as_an_independence_failure() -> None:
    registry = copy.deepcopy(REGISTRY)
    _google_serves_unknown_model(registry)
    errors = validate_registry(registry, GRAPH, ROOT)
    assert any(e.startswith("registry R17") for e in errors)
    assert not any(e.startswith("registry R14") for e in errors), errors


def test_resolve_family_takes_the_first_matching_pattern() -> None:
    provider = {
        "model_id": "gemini-3",
        "family_by_model_id": [
            {"pattern": "^gemini-", "family": "gemini"},
            {"pattern": "^gem", "family": "other"},
        ],
    }
    assert resolve_family(provider) == "gemini"
    assert resolve_family({**provider, "model_id": "claude-x"}) is None
    assert resolve_family({"family_by_model_id": provider["family_by_model_id"]}) is None
    assert resolve_family({"family": "qwen3"}) == "qwen3"
    assert (
        resolve_family({"model_id": "x", "family_by_model_id": [{"pattern": "(", "family": "x"}]})
        is None
    )
    # `re.match` anchors at the start: a pattern does not match in the middle of an id.
    assert (
        resolve_family(
            {"model_id": "x-gemini-1", "family_by_model_id": provider["family_by_model_id"]}
        )
        is None
    )


def test_candidate_backends_per_operation_are_pinned() -> None:
    """What each operation may run on, in preference order; a refactor must not change it."""
    got = {
        f"{stage}.{o['id']}": op_backends(REGISTRY, stage, o)
        for stage, o in operations(REGISTRY["stage_requirements"])
    }
    assert got == {
        "A1.proposal_a": ["claude_pro"],
        "A1.proposal_b": ["google_ai_pro"],
        "A2.critique_b_by_a": ["claude_pro"],
        "A2.critique_a_by_b": ["google_ai_pro"],
        "A3.synthesis": ["claude_pro", "google_ai_pro"],
        "A3R.synthesis_review": ["google_ai_pro", "claude_pro"],
        "C1.module_decomposition": ["qwen_local"],
        "D1.test_generation": ["qwen_local"],
        "E1.implementation_patch": ["qwen_local"],
    }
