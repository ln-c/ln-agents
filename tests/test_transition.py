"""Tests for transition authorization and edge-scoped budgets."""

from __future__ import annotations

from pathlib import Path

import pytest

from agentic_pipeline.graph import Edge, Graph, load_yaml
from agentic_pipeline.transition import (
    BudgetExhausted,
    BudgetLedger,
    ReservationState,
    TransitionDenied,
    check_transition,
    deny_all_guards,
)

ROOT = Path(__file__).resolve().parents[1]
GRAPH = Graph.load(ROOT / "pipeline" / "graph.yaml")
BUDGETS = load_yaml(ROOT / "policy" / "budgets.yaml")


def allow_all(edge: object, ctx: object) -> bool:
    return True


def test_existing_edge_allowed_by_guard() -> None:
    edge = check_transition(GRAPH, "A2", "blocking_flaw", "A1", allow_all, {})
    assert edge.budget == "design"


def test_deny_all_is_default_behaviour() -> None:
    with pytest.raises(TransitionDenied, match="not satisfied"):
        check_transition(GRAPH, "G4", "always", "A1", deny_all_guards, {})


@pytest.mark.parametrize(
    ("src", "guard", "to"),
    [("G4", "always", "C1"), ("A2", "always", "A1"), ("A1", "always", "B1")],
)
def test_missing_edge_denied(src: str, guard: str, to: str) -> None:
    with pytest.raises(TransitionDenied, match="no edge"):
        check_transition(GRAPH, src, guard, to, allow_all, {})


def test_leaving_human_state_denied() -> None:
    with pytest.raises(TransitionDenied, match="human decision"):
        check_transition(GRAPH, "H1", "revise_with_notes", "A1", allow_all, {})


def _edge(src: str, guard: str, to: str) -> Edge:
    return check_transition(GRAPH, src, guard, to, allow_all, {})


def test_unbudgeted_edge_needs_no_reservation() -> None:
    assert BudgetLedger.from_policy(BUDGETS).reserve(_edge("G4", "always", "A1")) is None


def test_budget_consumed_per_edge_until_exhausted() -> None:
    ledger = BudgetLedger.from_policy(BUDGETS)
    edge = _edge("A2", "blocking_flaw", "A1")
    for _ in range(BUDGETS["budgets"]["design"]["max_retries"]):
        ledger.reserve(edge).commit()
    with pytest.raises(BudgetExhausted):
        ledger.reserve(edge)


def test_release_returns_budget() -> None:
    ledger = BudgetLedger({"design": 1})
    edge = _edge("A2", "blocking_flaw", "A1")
    reservation = ledger.reserve(edge)
    with pytest.raises(BudgetExhausted):
        ledger.reserve(edge)  # outstanding reservation counts
    reservation.release()
    assert reservation.state is ReservationState.RELEASED
    assert ledger.reserve(edge) is not None and ledger.consumed.get("design", 0) == 0


def test_reservation_settles_once() -> None:
    reservation = BudgetLedger({"design": 2}).reserve(_edge("A2", "blocking_flaw", "A1"))
    reservation.commit()
    with pytest.raises(RuntimeError):
        reservation.release()
