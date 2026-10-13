"""The output contract: registry output_schema <-> prompts <-> gates."""

from __future__ import annotations

from pathlib import Path

import pytest

from agentic_pipeline.design_tasks import GUIDANCE, extract_json_object, json_task, load_schema_text
from agentic_pipeline.graph import load_yaml
from agentic_pipeline.schemas import schema_names

ROOT = Path(__file__).resolve().parents[1]
REGISTRY = load_yaml(ROOT / "policy" / "model_registry.yaml")


def operations() -> dict[str, dict]:
    return {
        f"{stage}.{op['id']}": op
        for stage, spec in REGISTRY["stage_requirements"].items()
        for op in spec.get("operations", [])
    }


def test_the_design_operations_declare_the_schemas_their_gates_read() -> None:
    """A2 reads critique.json and A4 reads proposal.json: the registry must ask for those."""
    declared = {k: op.get("output_schema") for k, op in operations().items()}
    assert declared["A1.proposal_a"] == declared["A1.proposal_b"] == "proposal"
    assert declared["A3.synthesis"] == "proposal"
    assert declared["A2.critique_b_by_a"] == declared["A2.critique_a_by_b"] == "critique"


def test_every_declared_schema_exists_and_has_guidance() -> None:
    for key, op in operations().items():
        name = op.get("output_schema")
        if name is not None:
            assert name in schema_names(), key
            assert name in GUIDANCE, f"{key}: no field guidance for {name}"


def test_task_embeds_role_stage_and_the_exact_schema() -> None:
    text = json_task("proposal_author", "A1", "proposal_a", "proposal")
    assert "proposal_author" in text and "A1" in text and "proposal_a" in text
    assert load_schema_text("proposal") in text


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ('{"a": 1}', {"a": 1}),
        ('```json\n{"a": 1}\n```', {"a": 1}),
        ('```\n{"a": 1}\n```', {"a": 1}),
        ("[1]", None),
        ("{", None),
        ('note {"a": 1}', None),  # prose around the object is not tolerated
        ('```json\n{"a": 1}', None),  # unclosed fence
    ],
)
def test_extract_json_object(text: str, expected: object) -> None:
    assert extract_json_object(text) == expected


def test_critique_task_carries_the_review_dimensions_and_severity_rules() -> None:
    """A critic that is not told the dimensions cannot honestly set review_complete."""
    text = json_task("cross_reviewer", "A2", "critique_b_by_a", "critique")
    for dimension in ("(1)", "(5)", "(9)", "observability", "idempotency", "testability"):
        assert dimension in text
    for severity in ("`blocking`", "`non_blocking`", "`informational`"):
        assert severity in text
    assert "Field rules:" in text


def test_every_task_has_field_rules_exactly_once() -> None:
    for schema in GUIDANCE:
        assert json_task("r", "S", "op", schema).count("Field rules:") == 1
