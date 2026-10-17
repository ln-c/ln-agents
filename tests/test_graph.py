"""Tests for pipeline graph loading and invariant enforcement."""

from __future__ import annotations

import copy
from pathlib import Path

from agentic_pipeline.graph import (
    Graph,
    load_yaml,
    spent_route,
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
    mutated["states"]["G"]["edges"].append({"to": "NONEXISTENT", "guard": "foo"})
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


def test_budgeted_retry_needs_a_spent_route() -> None:
    graph_dict = load_yaml(ROOT / "pipeline" / "graph.yaml")
    budgets = load_yaml(ROOT / "policy" / "budgets.yaml")
    routing = load_yaml(ROOT / "policy" / "routing.yaml")

    # X retries E1 on the `local` budget; local is exhausted to E3 via local_spent.
    mutated = copy.deepcopy(graph_dict)
    mutated["states"]["X"]["edges"] = [
        e for e in mutated["states"]["X"]["edges"] if e["guard"] != "local_spent"
    ]
    routing = copy.deepcopy(routing)
    del routing["routes"]["local_spent"]
    errors = validate(Graph.from_mapping(mutated), budgets, routing)
    assert any(e.startswith("X: budget 'local' has no spent route to E3") for e in errors)


def _rule9_errors(mutate) -> list[str]:  # noqa: ANN001
    graph_dict = copy.deepcopy(load_yaml(ROOT / "pipeline" / "graph.yaml"))
    mutate(graph_dict["states"])
    budgets = load_yaml(ROOT / "policy" / "budgets.yaml")
    routing = load_yaml(ROOT / "policy" / "routing.yaml")
    return [e for e in validate(Graph.from_mapping(graph_dict), budgets, routing) if "spent" in e]


def test_two_candidate_routes_need_exactly_one_spent_guard() -> None:
    def rename(states: dict) -> None:  # A4 -> H1: design_spent and constraint_sources_conflict
        for e in states["A4"]["edges"]:
            if e["guard"] == "design_spent":
                e["guard"] = "design_over"

    assert any(
        e.startswith("A4: budget 'design' has no spent route to H1") for e in _rule9_errors(rename)
    )


def test_a_budgeted_edge_is_not_a_spent_route() -> None:
    def charge(states: dict) -> None:
        for e in states["A2"]["edges"]:
            if e["guard"] == "design_spent":
                e["budget"] = "design"

    assert any(
        e.startswith("A2: budget 'design' has no spent route to H1") for e in _rule9_errors(charge)
    )


def test_spent_route_is_unique_for_every_budgeted_retry_in_the_real_graph() -> None:
    graph = Graph.load(ROOT / "pipeline" / "graph.yaml")
    budgets = load_yaml(ROOT / "policy" / "budgets.yaml")["budgets"]
    for e in graph.edges:
        if e.budget and e.src not in graph.human_states:
            route = spent_route(graph, e.src, budgets[e.budget]["on_exhausted"])
            assert route is not None and route.budget is None, e


def test_human_states_are_exempt_from_spent_routes() -> None:
    graph_dict = load_yaml(ROOT / "pipeline" / "graph.yaml")
    budgets = load_yaml(ROOT / "policy" / "budgets.yaml")
    routing = load_yaml(ROOT / "policy" / "routing.yaml")
    errors = validate(Graph.from_mapping(graph_dict), budgets, routing)
    assert not any(err.startswith("H1:") for err in errors)  # H1 -> A1 uses `design`


def mutated_errors(mutate) -> list[str]:  # type: ignore[no-untyped-def]
    raw = copy.deepcopy(load_yaml(ROOT / "pipeline" / "graph.yaml"))
    mutate(raw["states"])
    return validate(
        Graph.from_mapping(raw),
        load_yaml(ROOT / "policy" / "budgets.yaml"),
        load_yaml(ROOT / "policy" / "routing.yaml"),
    )


def test_merged_forks_by_a_side_effect_not_by_two_always_edges() -> None:
    graph = Graph.load(ROOT / "pipeline" / "graph.yaml")
    assert graph.side_effects == {"MERGED": [("merged", "L2")]}
    guards = [e.guard for e in graph.edges if e.src == "MERGED"]
    assert guards == ["merged", "abandoned"]  # distinct guards: the runner can always choose
    assert "L2" in graph.reachable(["MERGED"])  # a side-effect state still counts as reached


def test_a_side_effect_must_name_a_real_guard_and_a_code_state_that_ends_in_a_sink() -> None:
    def unknown_guard(states: dict) -> None:
        states["MERGED"]["side_effects"] = [{"when": "nope", "run": "L2"}]

    def missing_state(states: dict) -> None:
        states["MERGED"]["side_effects"] = [{"when": "merged", "run": "NOPE"}]

    def not_code(states: dict) -> None:
        states["MERGED"]["side_effects"] = [{"when": "merged", "run": "R2"}]

    def leads_on(states: dict) -> None:
        states["MERGED"]["side_effects"] = [{"when": "merged", "run": "M1"}]

    def also_entered(states: dict) -> None:
        states["RC"]["edges"].append({"to": "L2", "guard": "extra"})

    assert any("unknown guard" in e for e in mutated_errors(unknown_guard))
    assert any("does not exist" in e for e in mutated_errors(missing_state))
    assert any("must be a code state" in e for e in mutated_errors(not_code))
    assert any("lead only to sinks" in e for e in mutated_errors(leads_on))
    assert any("also be entered" in e for e in mutated_errors(also_entered))
