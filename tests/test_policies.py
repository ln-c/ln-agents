"""Tests for policy invariants in agentic_pipeline.policies."""

from __future__ import annotations

import copy
from pathlib import Path

from agentic_pipeline.graph import load_yaml
from agentic_pipeline.policies import (
    POLICY_FILES,
    check_data_classes,
    check_escalation,
    check_fast_path,
    check_vetoes,
    check_weights,
    validate_policies,
)

ROOT = Path(__file__).resolve().parents[1]
DOCS = {name: load_yaml(ROOT / "policy" / f"{name}.yaml") for name in POLICY_FILES}


def test_repo_policies_valid() -> None:
    errors = validate_policies(DOCS)
    assert not errors, errors


def test_weights_must_sum_to_one() -> None:
    doc = copy.deepcopy(DOCS["weights"])
    doc["weights"]["security"] = 0.5
    assert any("must sum to 1.0" in e for e in check_weights(doc))


def test_weights_must_be_positive() -> None:
    doc = copy.deepcopy(DOCS["weights"])
    doc["weights"]["cost"] = 0
    assert any("cost must be a positive number" in e for e in check_weights(doc))


def test_threshold_out_of_range() -> None:
    doc = copy.deepcopy(DOCS["weights"])
    doc["thresholds"]["near_tie_delta"] = 1.5
    assert any("near_tie_delta" in e for e in check_weights(doc))


def test_duplicate_veto_id() -> None:
    doc = copy.deepcopy(DOCS["vetoes"])
    doc["vetoes"].append(dict(doc["vetoes"][0]))
    assert any("duplicate id" in e for e in check_vetoes(doc))


def test_data_class_order_drift() -> None:
    doc = copy.deepcopy(DOCS["data_classes"])
    doc["order"].remove("restricted")
    assert any("order" in e for e in check_data_classes(doc))


def test_confidential_must_block_cloud() -> None:
    doc = copy.deepcopy(DOCS["data_classes"])
    doc["classes"]["confidential"]["cloud_allowed"] = True
    assert any("confidential must have cloud_allowed: false" in e for e in check_data_classes(doc))


def test_cloud_escalation_cannot_allow_confidential() -> None:
    doc = copy.deepcopy(DOCS["escalation"])
    cloud = next(a for a in doc["agents"] if a["location"] == "cloud")
    cloud["allowed_data_classes"].append("confidential")
    errors = check_escalation(doc, DOCS["data_classes"])
    assert any("cloud agent allows non-cloud class 'confidential'" in e for e in errors)


def test_escalation_unknown_data_class() -> None:
    doc = copy.deepcopy(DOCS["escalation"])
    doc["agents"][0]["allowed_data_classes"].append("top_secret")
    errors = check_escalation(doc, DOCS["data_classes"])
    assert any("unknown data class 'top_secret'" in e for e in errors)


def test_fast_path_require_all_not_empty() -> None:
    doc = copy.deepcopy(DOCS["fast_path"])
    doc["require_all"] = []
    assert any("require_all must not be empty" in e for e in check_fast_path(doc))


def test_fast_path_requires_veto_check() -> None:
    doc = copy.deepcopy(DOCS["fast_path"])
    doc["require_all"].remove("no_active_veto")
    assert any("no_active_veto" in e for e in check_fast_path(doc))
