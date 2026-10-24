"""What an operation's `tool_profile` and `tool_reads` promise in the registry, checked against
policy/tools.yaml and the data classes, and the dispatcher's refusal while an adapter cannot
provide tools."""

from __future__ import annotations

import copy
import shutil
from pathlib import Path
from typing import Any

import pytest
import yaml
from test_dispatch import BINDABLE, FakeAdapter, make

from agentic_pipeline import mcp_policy as mp
from agentic_pipeline.dispatch import DispatchBlocked, adapter_blockers
from agentic_pipeline.validation import validate_repo

ROOT = Path(__file__).resolve().parents[1]
REGISTRY = yaml.safe_load((ROOT / "policy" / "model_registry.yaml").read_text())
# The policy without the path rules the repository gives its own files: these tests set theirs.
DATA_CLASSES = {
    key: value
    for key, value in yaml.safe_load((ROOT / "policy" / "data_classes.yaml").read_text()).items()
    if key != "path_rules"
}
TOOLS = yaml.safe_load((ROOT / "policy" / "tools.yaml").read_text())
TOOLS["profiles"]["cloud_contracts"] = {
    "destination": "cloud_service",
    "tools": ["repo_list", "repo_read", "repo_search"],
    "limits": {"max_read_bytes": 65536, "max_calls": 50},
}
BUILD_READS = ["pinned_source_tree", "applicable_contracts", "protected_tests"]


def operation(registry: dict[str, Any], dotted: str) -> dict[str, Any]:
    stage, op_id = dotted.split(".")
    return next(o for o in registry["stage_requirements"][stage]["operations"] if o["id"] == op_id)


def registry_with(**ops: dict[str, Any]) -> dict[str, Any]:
    registry = copy.deepcopy(REGISTRY)
    for dotted, fields in ops.items():
        operation(registry, dotted.replace("__", ".")).update(fields)
    return registry


def check(registry: dict[str, Any], tools: Any = TOOLS) -> list[str]:
    return mp.check_operation_tools(registry, tools, DATA_CLASSES)


def test_the_registry_as_it_is_promises_no_tools() -> None:
    assert check(REGISTRY) == []


def test_the_build_operations_may_have_local_tools_over_the_pinned_snapshot() -> None:
    fields = {"tool_profile": "local_project", "tool_reads": BUILD_READS}
    assert (
        check(registry_with(C1__module_decomposition=fields, E1__implementation_patch=fields)) == []
    )


def test_a_cloud_operation_may_have_a_cloud_profile_with_the_contracts_to_read() -> None:
    registry = registry_with(
        A1__proposal_a={"tool_profile": "cloud_contracts", "tool_reads": ["applicable_contracts"]}
    )
    assert check(registry) == []
    # cloud_pipeline_info lists run_status, which a design operation with session separation
    # may not have; a synthesis may.
    assert check(registry_with(A3__synthesis={"tool_profile": "cloud_pipeline_info"})) == []
    assert any(
        "session separation" in e
        for e in check(registry_with(A1__proposal_a={"tool_profile": "cloud_pipeline_info"}))
    )


@pytest.mark.parametrize(
    ("ops", "message"),
    [
        (
            {"E1__implementation_patch": {"tool_profile": "nope"}},
            "which policy/tools.yaml does not have",
        ),
        # a model outside the host with a profile that is inside it
        (
            {
                "A1__proposal_a": {
                    "tool_profile": "local_project",
                    "tool_reads": ["applicable_contracts"],
                }
            },
            "outside the host, but profile local_project has destination local",
        ),
        (
            {
                "A3R__synthesis_review": {
                    "tool_profile": "local_project",
                    "tool_reads": ["synthesis_sealed"],
                }
            },
            "outside the host",
        ),
        # tool_reads
        (
            {
                "E1__implementation_patch": {
                    "tool_profile": "local_project",
                    "tool_reads": ["nothing"],
                }
            },
            "not a sealed catalog artifact",
        ),
        (
            {
                "E1__implementation_patch": {
                    "tool_profile": "local_project",
                    "tool_reads": ["implementation_patch"],
                }
            },
            "not a sealed catalog artifact",
        ),
        (
            {
                "A1__proposal_a": {
                    "tool_profile": "cloud_contracts",
                    "tool_reads": ["proposal_b_sealed"],
                    "denied_reads": [],  # so only the independence rule can object
                }
            },
            "produced by another operation of A1",
        ),
        (
            {
                "E1__implementation_patch": {
                    "tool_profile": "local_project",
                    "tool_reads": ["pinned_source_tree"],
                    "denied_reads": ["pinned_source_tree"],
                }
            },
            "also in denied_reads",
        ),
        (
            {
                "C1__module_decomposition": {
                    "tool_profile": "local_project",
                    "tool_reads": ["contracts_sealed"],
                }
            },
            "which it produces",
        ),
        # repository tools and tool_reads go together
        ({"E1__implementation_patch": {"tool_profile": "local_project"}}, "no tool_reads to serve"),
        (
            {
                "A1__proposal_a": {
                    "tool_profile": "cloud_pipeline_info",
                    "tool_reads": ["applicable_contracts"],
                }
            },
            "lists no repository tool",
        ),
        # nothing the destination may receive
        (
            {
                "A1__proposal_a": {
                    "tool_profile": "cloud_contracts",
                    "tool_reads": ["pinned_source_tree"],
                }
            },
            "may not receive",
        ),
    ],
)
def test_an_operation_that_contradicts_its_policy_is_reported(
    ops: dict[str, dict[str, Any]], message: str
) -> None:
    errors = check(registry_with(**ops))
    assert any(message in e for e in errors), errors


def test_a_design_operation_with_session_separation_gets_no_run_tools() -> None:
    tools = copy.deepcopy(TOOLS)
    tools["profiles"]["cloud_pipeline_info"]["tools"].append("run_evidence")
    registry = registry_with(A1__proposal_a={"tool_profile": "cloud_pipeline_info"})
    assert any("session separation" in e and "run_evidence" in e for e in check(registry, tools))
    # An operation without session separation may have them (A3 synthesis, for one).
    assert check(registry_with(A3__synthesis={"tool_profile": "cloud_pipeline_info"}), tools) == []


def test_a_cloud_destination_with_a_derived_artifact_is_judged_when_the_run_has_its_class() -> None:
    # Derived artifacts have no default class, so only the run can say; the registry is accepted.
    registry = registry_with(
        A3R__synthesis_review={
            "tool_profile": "cloud_contracts",
            "tool_reads": ["synthesis_sealed"],
        }
    )
    assert check(registry) == []


def with_path_rules(*rules: tuple[str, str], **defaults: str) -> dict[str, Any]:
    doc = copy.deepcopy(DATA_CLASSES)
    doc["path_rules"] = [{"path": path, "class": cls} for path, cls in rules]
    doc["artifact_defaults"].update(defaults)
    return doc


def test_a_path_rule_can_make_a_pinned_artifact_receivable_by_a_cloud_destination() -> None:
    message = "which a cloud_service destination may not receive"

    def reading(artifact: str) -> dict[str, Any]:
        return registry_with(
            A1__proposal_a={"tool_profile": "cloud_contracts", "tool_reads": [artifact]}
        )

    def errors(artifact: str, classes: dict[str, Any]) -> bool:
        return any(
            message in e for e in mp.check_operation_tools(reading(artifact), TOOLS, classes)
        )

    assert errors("pinned_source_tree", DATA_CLASSES)
    assert not errors("pinned_source_tree", with_path_rules(("docs/**", "internal")))
    # a rule that gives the files a class the destination may not receive changes nothing
    assert errors("pinned_source_tree", with_path_rules(("docs/**", "confidential")))
    # a rule counts for the artifact it can reach, by where the artifact sits in the repository
    assert errors("protected_tests", with_path_rules(("docs/**", "internal")))
    assert not errors("protected_tests", with_path_rules(("tests/fixtures/**", "internal")))
    shared = with_path_rules(("docs/**", "internal"), applicable_contracts="confidential")
    assert errors("applicable_contracts", shared)
    shared["path_rules"].append({"path": "contracts/public/**", "class": "internal"})
    assert not errors("applicable_contracts", shared)
    # a rule is about the repository, not about an artifact kept under its own id
    classes = with_path_rules(("**", "internal"), requirements="confidential")
    assert errors("requirements", classes)
    # rules the code cannot read count for nothing here (validate reports them)
    broken = with_path_rules(("docs/", "internal"))
    assert errors("pinned_source_tree", broken)


def test_an_agent_may_name_a_profile_and_must_fit_its_models() -> None:
    registry = copy.deepcopy(REGISTRY)
    registry["agents"]["grounding"]["tool_profile"] = "cloud_pipeline_info"
    assert check(registry) == []
    registry["agents"]["grounding"]["tool_profile"] = "local_project"
    assert any("agents.grounding" in e and "outside the host" in e for e in check(registry))
    registry["agents"]["grounding"]["tool_profile"] = "missing"
    assert any("agents.grounding names profile 'missing'" in e for e in check(registry))


def test_a_profile_is_named_but_there_is_no_tools_file() -> None:
    registry = registry_with(
        E1__implementation_patch={"tool_profile": "local_project", "tool_reads": BUILD_READS}
    )
    assert any("there is no policy/tools.yaml" in e for e in check(registry, None))
    assert check(REGISTRY, None) == []


# ── validate ─────────────────────────────────────────────────────────────────


def project(tmp_path: Path, registry: dict[str, Any], tools: dict[str, Any] | None = TOOLS) -> Path:
    root = tmp_path / "repo"
    for name in ("pipeline", "policy", "schemas", "agents"):
        shutil.copytree(ROOT / name, root / name)
    (root / "policy" / "model_registry.yaml").write_text(yaml.safe_dump(registry, sort_keys=False))
    if tools is None:
        (root / "policy" / "tools.yaml").unlink()
    else:
        (root / "policy" / "tools.yaml").write_text(yaml.safe_dump(tools, sort_keys=False))
    return root


def test_validate_reports_a_registry_that_contradicts_the_tools_policy(tmp_path: Path) -> None:
    bad = registry_with(
        A1__proposal_a={"tool_profile": "local_project", "tool_reads": ["applicable_contracts"]}
    )
    errors = validate_repo(project(tmp_path, bad))
    assert any("A1.proposal_a" in e and "outside the host" in e for e in errors), errors
    good = registry_with(
        E1__implementation_patch={"tool_profile": "local_project", "tool_reads": BUILD_READS}
    )
    assert validate_repo(project(tmp_path / "again", good)) == []


def test_a_runs_frozen_copy_from_before_tools_yaml_is_validated_without_it(tmp_path: Path) -> None:
    frozen = project(tmp_path, REGISTRY, tools=None)
    assert validate_repo(ROOT, policy_root=frozen) == []
    # but a frozen registry that promises tools needs the file
    promising = registry_with(
        E1__implementation_patch={"tool_profile": "p", "tool_reads": BUILD_READS}
    )
    frozen = project(tmp_path / "other", promising, tools=None)
    assert any("no policy/tools.yaml" in e for e in validate_repo(ROOT, policy_root=frozen))


# ── the dispatcher ───────────────────────────────────────────────────────────

ADAPTER_FIELDS = {"tool_profile": "local_project", "tool_reads": BUILD_READS}


class ToolAdapter(FakeAdapter):
    provides_tools = True


def test_dispatch_names_an_operation_whose_adapter_cannot_provide_the_tools_it_is_promised() -> (
    None
):
    registry = registry_with(E1__implementation_patch=ADAPTER_FIELDS)
    backends = {"implementation_patch": "qwen3_5_9b"}
    found = adapter_blockers(registry, "E1", backends, {"qwen3_5_9b": FakeAdapter()})
    assert found == [
        "E1.implementation_patch: the registry gives it MCP tools (tool_profile local_project) "
        "but the qwen3_5_9b adapter cannot provide tools"
    ]
    assert adapter_blockers(registry, "E1", backends, {"qwen3_5_9b": ToolAdapter()}) == []
    assert adapter_blockers(REGISTRY, "E1", backends, {"qwen3_5_9b": FakeAdapter()}) == []


def test_the_dispatcher_refuses_such_an_operation_before_any_call(tmp_path: Path) -> None:
    adapters = {b: FakeAdapter() for b in BINDABLE}
    dispatcher = make(tmp_path, adapters)
    dispatcher.registry = registry_with(E1__implementation_patch=ADAPTER_FIELDS)
    with pytest.raises(DispatchBlocked, match="cannot provide tools"):
        dispatcher.dispatch_stage("E0", "always", "E1", {})
    assert adapters["qwen3_5_9b"].grants == []
    assert not (dispatcher.run_root / "attempts").exists()


def test_the_dispatcher_runs_the_operation_once_its_adapter_provides_tools(tmp_path: Path) -> None:
    adapters: dict[str, FakeAdapter] = {b: FakeAdapter() for b in BINDABLE}
    adapters["qwen3_5_9b"] = ToolAdapter()
    dispatcher = make(tmp_path, adapters)
    dispatcher.registry = registry_with(E1__implementation_patch=ADAPTER_FIELDS)
    dispatcher.dispatch_stage("E0", "always", "E1", {})
    assert len(adapters["qwen3_5_9b"].grants) == 1
