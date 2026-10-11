"""Tests for policy definitions and invariants."""

from __future__ import annotations

import math
from pathlib import Path

from agentic_pipeline.graph import load_yaml

ROOT = Path(__file__).resolve().parents[1]
POLICY_DIR = ROOT / "policy"


def test_budgets_policy() -> None:
    data = load_yaml(POLICY_DIR / "budgets.yaml")
    assert data["version"] == 1
    budgets = data["budgets"]
    expected_keys = {
        "run",
        "design",
        "regen",
        "evidence",
        "local",
        "escalation",
        "test",
        "infra",
        "reconcile",
        "remediation",
        "runner_up",
    }
    assert expected_keys.issubset(set(budgets.keys()))
    for _key, spec in budgets.items():
        assert spec["max_retries"] == 2
        assert "on_exhausted" in spec


def test_weights_sum_to_one() -> None:
    data = load_yaml(POLICY_DIR / "weights.yaml")
    weights = data["weights"]
    total = sum(weights.values())
    assert math.isclose(total, 1.0, rel_tol=1e-5), f"Weights sum to {total}, expected 1.0"
    assert "near_tie_delta" in data["thresholds"]
    assert "high_uncertainty" in data["thresholds"]


def test_vetoes_defined() -> None:
    data = load_yaml(POLICY_DIR / "vetoes.yaml")
    vetoes = data["vetoes"]
    assert len(vetoes) >= 5
    ids = {v["id"] for v in vetoes}
    assert "schema_invalid" in ids
    assert "violates_accepted_adr" in ids
    assert "contract_break" in ids


def test_data_classes_block_unknown_from_cloud() -> None:
    data = load_yaml(POLICY_DIR / "data_classes.yaml")
    classes = data["classes"]
    assert classes["unknown"]["cloud_allowed"] is False
    assert classes["confidential"]["cloud_allowed"] is False
    assert classes["restricted"]["cloud_allowed"] is False
    assert classes["public"]["cloud_allowed"] is True
    assert classes["internal"]["cloud_allowed"] is True


def test_fast_path_empty_allowlist_by_default() -> None:
    data = load_yaml(POLICY_DIR / "fast_path.yaml")
    assert data["task_class_allowlist"] == []
    assert len(data["require_all"]) >= 8
