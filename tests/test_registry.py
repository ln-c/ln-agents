"""Tests for model registry invariants (R1-R18)."""
# ruff: noqa: E501

from __future__ import annotations

import copy
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from agentic_pipeline.graph import Graph, load_yaml
from agentic_pipeline.registry import (
    backend_capabilities,
    backend_family,
    backend_location,
    op_backends,
    operations,
    pattern_family,
    validate_registry,
)

ROOT = Path(__file__).resolve().parents[1]
REGISTRY = load_yaml(ROOT / "policy" / "model_registry.yaml")
GRAPH = Graph.load(ROOT / "pipeline" / "graph.yaml")

type Registry = dict[str, Any]


def op(reg: Registry, stage: str, op_id: str) -> dict[str, Any]:
    return next(o for o in reg["stage_requirements"][stage]["operations"] if o["id"] == op_id)


def _swap_proposal_a_backend(r: Registry) -> None:
    r["agents"]["design"]["proposal_a"]["primary"] = "gemini_3_8_flash_high"


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
    r["stage_requirements"]["G"] = {"mode": "disabled", "reason": "not a model state"}


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
    r["agents"]["build"]["primary"] = "clef_flash"


def _no_capabilities_declared(r: Registry) -> None:
    r["models"]["qwen3_5_9b"]["capabilities"] = []


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


def _design_b_on_claude_via_agy(r: Registry) -> None:
    r["models"]["claude_sonnet_5_5_high"]["capabilities"] = ["design_reasoning"]
    r["agents"]["design"]["proposal_b"]["primary"] = "claude_sonnet_5_5_high"


def _model_id_matches_no_pattern(r: Registry) -> None:
    r["models"]["gemini_3_8_flash_high"]["model_id"] = "mystery-1"


def _mislabelled_family(r: Registry) -> None:
    r["models"]["claude_opus_5_5_low"]["family"] = "gemini"


def _model_on_a_non_model_provider(r: Registry) -> None:
    r["models"]["qwen3_5_9b"]["provider"] = "deterministic_rules"


def _duplicate_model(r: Registry) -> None:
    r["models"]["gemini_twin"] = dict(r["models"]["gemini_3_8_flash_high"])


def _claude_model_declared_gemini(r: Registry) -> None:
    r["models"]["claude_cli_default"]["family"] = "gemini"


def _llamacpp_model_with_no_pattern(r: Registry) -> None:
    r["models"]["llama3"] = {
        "provider": "llamacpp_local",
        "model_id": "llama3",
        "family": "llama",
        "capabilities": [],
    }


def _template_defaults_not_an_object(r: Registry) -> None:
    r["models"]["qwen3_5_9b"]["chat_template_kwargs"] = "enable_thinking=false"


def _provider_without_patterns(r: Registry) -> None:
    del r["providers"]["clef_flash_free"]["family_patterns"]


def _openvino_qwen_declared_clef(r: Registry) -> None:
    r["models"]["openvino_qwen3_8b_int4_cw"]["family"] = "clef"


def _binds_a_provider(r: Registry) -> None:
    r["agents"]["build"]["primary"] = "llamacpp_local"


def _bad_family_pattern(r: Registry) -> None:
    r["providers"]["google_ai_pro"]["family_patterns"].insert(0, {"pattern": "(", "family": "x"})


def _bad_pattern_after_a_match(r: Registry) -> None:
    r["providers"]["google_ai_pro"]["family_patterns"].append({"pattern": "(", "family": "x"})


def _cloud_declared_local(r: Registry) -> None:
    r["providers"]["claude_pro"]["location"] = "local"


def _local_declared_cloud(r: Registry) -> None:
    r["providers"]["llamacpp_local"]["location"] = "cloud"


def _unknown_access_mode(r: Registry) -> None:
    r["providers"]["llamacpp_local"]["access_mode"] = "carrier_pigeon"


def _no_location(r: Registry) -> None:
    del r["providers"]["claude_pro"]["location"]


CASES: list[tuple[str, Callable[[Registry], None], str]] = [
    ("schema: denylist floor", _drop_denylist_entry, "registry R1"),
    ("path escapes root", _escape_spec, "registry R2"),
    ("path missing", _missing_spec, "registry R2"),
    ("unknown provider", _unknown_provider, "registry R3"),
    ("model state unmapped", _drop_bench, "registry R4 BENCH"),
    ("code state mapped", _map_code_state, "registry R4 G"),
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
    (
        "design B on a claude model via agy",
        _design_b_on_claude_via_agy,
        "registry R14 A1.proposal_b",
    ),
    ("binds a provider, not a model", _binds_a_provider, "registry R3 agents.build.primary"),
    ("schema: location required", _no_location, "registry R1"),
    ("cloud mode declared local", _cloud_declared_local, "registry R16 providers.claude_pro"),
    ("local mode declared cloud", _local_declared_cloud, "registry R16 providers.llamacpp_local"),
    ("unknown access mode", _unknown_access_mode, "registry R16 providers.llamacpp_local"),
    ("model id matches no pattern", _model_id_matches_no_pattern, "registry R17 models.gemini_3"),
    (
        "family disagrees with the id",
        _mislabelled_family,
        "registry R17 models.claude_opus_5_5_low",
    ),
    (
        "model on a non-model provider",
        _model_on_a_non_model_provider,
        "registry R17 models.qwen3_5_9b",
    ),
    ("duplicate provider and model_id", _duplicate_model, "registry R17 models.gemini_twin"),
    (
        "claude model declared gemini",
        _claude_model_declared_gemini,
        "registry R17 models.claude_cli_default",
    ),
    (
        "llama.cpp model with no pattern",
        _llamacpp_model_with_no_pattern,
        "registry R17 models.llama3",
    ),
    (
        "template defaults that are not an object",
        _template_defaults_not_an_object,
        "registry R1",
    ),
    (
        "provider without patterns",
        _provider_without_patterns,
        "registry R17 providers.clef_flash_free",
    ),
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


def test_an_unclassifiable_model_is_reported_once_not_as_an_independence_failure() -> None:
    registry = copy.deepcopy(REGISTRY)
    _model_id_matches_no_pattern(registry)
    errors = validate_registry(registry, GRAPH, ROOT)
    assert any(e.startswith("registry R17") for e in errors)
    assert not any(e.startswith("registry R14") for e in errors), errors


def test_pattern_family_takes_the_first_matching_pattern() -> None:
    provider = {
        "family_patterns": [
            {"pattern": "^gemini-", "family": "gemini"},
            {"pattern": "^gem", "family": "other"},
        ]
    }
    assert pattern_family(provider, "gemini-3") == "gemini"
    assert pattern_family(provider, "claude-x") is None
    assert pattern_family({}, "gemini-3") is None
    assert pattern_family({"family_patterns": [{"pattern": "(", "family": "x"}]}, "x") is None
    # `re.match` anchors at the start: a pattern does not match in the middle of an id.
    assert pattern_family(provider, "x-gemini-1") is None


CLAUDE, GEMINI, QWEN = "claude_cli_default", "gemini_3_8_flash_high", "qwen3_5_9b"


def test_candidate_models_per_operation_are_pinned() -> None:
    """What each operation may run on, in preference order; a refactor must not change it."""
    got = {
        f"{stage}.{o['id']}": op_backends(REGISTRY, stage, o)
        for stage, o in operations(REGISTRY["stage_requirements"])
    }
    assert got == {
        "A1.proposal_a": [CLAUDE],
        "A1.proposal_b": [GEMINI],
        "A2.critique_b_by_a": [CLAUDE],
        "A2.critique_a_by_b": [GEMINI],
        "A3.synthesis": [CLAUDE, GEMINI],
        "A3R.synthesis_review": [GEMINI, CLAUDE],
        "C1.module_decomposition": [QWEN],
        "D1.test_generation": [QWEN],
        "E1.implementation_patch": [QWEN],
    }


def test_each_candidates_resolved_properties_are_pinned() -> None:
    """(provider, model id, family, location, capabilities): same as before the catalog."""
    resolved = {
        m: (
            REGISTRY["models"][m]["provider"],
            REGISTRY["models"][m]["model_id"],
            backend_family(REGISTRY, m),
            backend_location(REGISTRY, m),
            sorted(backend_capabilities(REGISTRY, m)),
        )
        for m in (CLAUDE, GEMINI, QWEN)
    }
    assert resolved == {
        CLAUDE: ("claude_pro", "cli-default", "claude", "cloud", ["code_generation", "design_reasoning"]),
        GEMINI: ("google_ai_pro", "gemini-3.8-flash-high", "gemini", "cloud", ["code_generation", "design_reasoning"]),
        QWEN: ("llamacpp_local", "qwen3.5-9b", "qwen3_5", "local", ["classification", "code_generation"]),
    }  # fmt: skip


AGY_MODELS = {  # `agy models`, as listed by the tool
    "gemini-4-argon-medium": "gemini",
    "gemini-4-argon-high": "gemini",
    "gemini-4-argon-low": "gemini",
    "gemini-3.8-flash-high": "gemini",
    "gemini-3.8-flash-medium": "gemini",
    "gemini-3.8-flash-low": "gemini",
    "claude-opus-5-5-low": "claude",
    "claude-opus-5-5-medium": "claude",
    "claude-opus-5-5-high": "claude",
    "claude-sonnet-5-5-low": "claude",
    "claude-sonnet-5-5-medium": "claude",
    "claude-sonnet-5-5-high": "claude",
    "gpt-oss-120b-medium": "gpt_oss",
}


def test_the_catalog_holds_every_model_agy_lists_with_the_right_family() -> None:
    catalog = {
        m["model_id"]: m["family"]
        for m in REGISTRY["models"].values()
        if m["provider"] == "google_ai_pro"
    }
    assert catalog == AGY_MODELS


@pytest.mark.parametrize(("model_id", "family"), AGY_MODELS.items())
def test_every_model_agy_lists_matches_its_family_pattern(model_id: str, family: str) -> None:
    assert pattern_family(REGISTRY["providers"]["google_ai_pro"], model_id) == family


def test_known_but_unclaimed_models_cannot_be_bound() -> None:
    unclaimed = [m for m, v in REGISTRY["models"].items() if not v["capabilities"]]
    agy = [m for m, v in REGISTRY["models"].items() if v["provider"] == "google_ai_pro"]
    openvino = [m for m, v in REGISTRY["models"].items() if v["provider"] == "openvino_local"]
    assert sorted(unclaimed) == sorted([*(set(agy) - {GEMINI}), *openvino])
    for model in unclaimed:
        registry = copy.deepcopy(REGISTRY)
        registry["agents"]["design"]["proposal_b"]["primary"] = model
        errors = validate_registry(registry, GRAPH, ROOT)
        assert any(e.startswith("registry R18 A1.proposal_b") for e in errors), (model, errors)


@pytest.mark.parametrize(
    "model",
    [
        m
        for m, v in REGISTRY["models"].items()
        if v["provider"] == "google_ai_pro" and v["family"] != "gemini"
    ],
)
def test_design_b_cannot_use_a_claude_model_agy_serves_but_can_use_another_family(
    model: str,
) -> None:
    registry = copy.deepcopy(REGISTRY)
    registry["models"][model]["capabilities"] = ["code_generation", "design_reasoning"]
    registry["agents"]["design"]["proposal_b"]["primary"] = model
    errors = validate_registry(registry, GRAPH, ROOT)
    if registry["models"][model]["family"] == "claude":  # shares a family with design A
        assert any(e.startswith("registry R14 A1.proposal_b") for e in errors), errors
    else:  # a family of its own is independent of design A
        assert not errors, errors
