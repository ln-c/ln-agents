"""Tests for pipeline graph loading and invariant enforcement."""

from __future__ import annotations

import copy
from pathlib import Path

from agentic_pipeline.graph import (
    Graph,
    load_yaml,
    validate,
    validate_repo,
)

ROOT = Path(__file__).resolve().parents[1]


def test_repo_graph_valid() -> None:
    errors = validate_repo(ROOT)
    assert not errors, f"Repository graph validation failed: {errors}"


def test_detects_unbounded_loop_when_budget_missing() -> None:
    graph_dict = load_yaml(ROOT / "pipeline" / "graph.yaml")
    budgets = load_yaml(ROOT / "policy" / "budgets.yaml")
    routing = load_yaml(ROOT / "policy" / "routing.yaml")

    # Strip budget from the runner_up edge (X -> A4)
    mutated = copy.deepcopy(graph_dict)
    for edge in mutated["states"]["X"]["edges"]:
        if edge["to"] == "A4":
            edge.pop("budget", None)

    bad_graph = Graph.from_mapping(mutated)
    errors = validate(bad_graph, budgets, routing)
    assert any("unbounded loop" in err and "A4" in err for err in errors)


def test_detects_unknown_edge_target() -> None:
    graph_dict = load_yaml(ROOT / "pipeline" / "graph.yaml")
    budgets = load_yaml(ROOT / "policy" / "budgets.yaml")
    routing = load_yaml(ROOT / "policy" / "routing.yaml")

    mutated = copy.deepcopy(graph_dict)
    mutated["states"]["G1"]["edges"].append({"to": "NONEXISTENT", "guard": "foo"})
    bad_graph = Graph.from_mapping(mutated)
    errors = validate(bad_graph, budgets, routing)
    assert any("NONEXISTENT" in err and "does not exist" in err for err in errors)


def test_guarded_path_violation() -> None:
    graph_dict = load_yaml(ROOT / "pipeline" / "graph.yaml")
    budgets = load_yaml(ROOT / "policy" / "budgets.yaml")
    routing = load_yaml(ROOT / "policy" / "routing.yaml")

    # Add shortcut from A1 directly to C1, bypassing B1 and H1
    mutated = copy.deepcopy(graph_dict)
    mutated["states"]["A1"]["edges"].append({"to": "C1", "guard": "bypass"})
    bad_graph = Graph.from_mapping(mutated)
    errors = validate(bad_graph, budgets, routing)
    assert any("C1 reachable while bypassing" in err for err in errors)


def test_terminal_without_provenance_flagged() -> None:
    graph_dict = load_yaml(ROOT / "pipeline" / "graph.yaml")
    budgets = load_yaml(ROOT / "policy" / "budgets.yaml")
    routing = load_yaml(ROOT / "policy" / "routing.yaml")

    mutated = copy.deepcopy(graph_dict)
    mutated["states"]["T-RELEASED"]["writes"] = []
    bad_graph = Graph.from_mapping(mutated)
    errors = validate(bad_graph, budgets, routing)
    assert any("T-RELEASED: terminal state must write provenance to L1" in err for err in errors)


def test_router_must_not_have_may_modify() -> None:
    graph_dict = load_yaml(ROOT / "pipeline" / "graph.yaml")
    budgets = load_yaml(ROOT / "policy" / "budgets.yaml")
    routing = load_yaml(ROOT / "policy" / "routing.yaml")

    mutated = copy.deepcopy(graph_dict)
    mutated["states"]["X"]["may_modify"] = ["contracts"]
    bad_graph = Graph.from_mapping(mutated)
    errors = validate(bad_graph, budgets, routing)
    assert any("X: router must declare may_modify: []" in err for err in errors)
