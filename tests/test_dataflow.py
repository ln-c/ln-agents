"""Data-flow judgement behind the data_class_violation veto."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from agentic_pipeline.dataflow import candidate_verdict, flow_verdict
from agentic_pipeline.graph import load_yaml
from agentic_pipeline.policies import check_data_classes
from agentic_pipeline.schemas import SCHEMA_DIR

POLICY: dict[str, Any] = {
    "order": ["public", "internal", "confidential", "restricted", "unknown"],
    "classes": {
        "public": {"cloud_allowed": True},
        "internal": {"cloud_allowed": True},
        "confidential": {"cloud_allowed": False},
        "restricted": {"cloud_allowed": False},
        "unknown": {"cloud_allowed": False},
    },
    "destinations": {
        "local": {"cloud": False},
        "internal_service": {"cloud": False},
        "cloud_service": {"cloud": True},
        "third_party": {"cloud": True},
    },
}
SPEC = {"declared_classification": None, "unresolved_classification": [], "known_flows": []}


def f(data_class: str, destination: str) -> dict[str, str]:
    return {"id": "f", "data_class": data_class, "destination": destination}


@pytest.mark.parametrize(
    ("data_class", "destination", "expected"),
    [
        ("public", "cloud_service", False),
        ("internal", "third_party", False),
        ("confidential", "cloud_service", True),
        ("restricted", "third_party", True),
        ("unknown", "cloud_service", True),
        ("restricted", "local", False),
        ("confidential", "internal_service", False),
    ],
)
def test_flows_follow_cloud_allowed(data_class: str, destination: str, expected: bool) -> None:
    assert flow_verdict(f(data_class, destination), POLICY, SPEC)[0] is expected


def test_undefined_names_are_undecided_never_clear() -> None:
    assert flow_verdict(f("secret", "local"), POLICY, SPEC)[0] is None
    assert flow_verdict(f("public", "mars"), POLICY, SPEC)[0] is None
    assert (
        flow_verdict(
            f("public", "cloud_service"), POLICY, {**SPEC, "declared_classification": "x"}
        )[0]
        is None
    )


def test_the_entry_spec_can_only_raise_the_class() -> None:
    raised = {**SPEC, "declared_classification": "restricted"}
    assert flow_verdict(f("public", "cloud_service"), POLICY, raised)[0] is True
    lowered = {**SPEC, "declared_classification": "public"}
    assert flow_verdict(f("confidential", "cloud_service"), POLICY, lowered)[0] is True


def test_unresolved_classification_blocks_a_clear_but_not_a_violation() -> None:
    pending = {**SPEC, "unresolved_classification": ["customer export"]}
    assert flow_verdict(f("internal", "cloud_service"), POLICY, pending)[0] is None
    assert flow_verdict(f("confidential", "cloud_service"), POLICY, pending)[0] is True
    assert flow_verdict(f("internal", "local"), POLICY, pending)[0] is False


def test_candidate_verdicts() -> None:
    assert candidate_verdict({}, POLICY, SPEC) is None
    assert candidate_verdict({"data_flows": []}, POLICY, SPEC) is False
    mixed = {"data_flows": [f("public", "local"), f("public", "mars")]}
    assert candidate_verdict(mixed, POLICY, SPEC) is None
    both = {"data_flows": [f("public", "mars"), f("restricted", "third_party")]}
    assert candidate_verdict(both, POLICY, SPEC) is True


def test_policy_destinations_match_the_proposal_schema() -> None:
    schema = json.loads((SCHEMA_DIR / "proposal.schema.json").read_text())
    enum = schema["properties"]["data_flows"]["items"]["properties"]["destination"]["enum"]
    repo = load_yaml(Path(__file__).resolve().parents[1] / "policy" / "data_classes.yaml")
    assert sorted(enum) == sorted(repo["destinations"])


def test_destinations_are_validated() -> None:
    assert check_data_classes(POLICY) == []
    for broken in ({}, {"local": {"cloud": "yes"}}):
        doc = copy.deepcopy(POLICY)
        doc["destinations"] = broken
        assert any("destinations" in e for e in check_data_classes(doc))
