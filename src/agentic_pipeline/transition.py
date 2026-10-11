"""Transition authorization and edge-scoped retry budgets.

`check_transition` is non-mutating: it confirms the edge exists in the graph,
that it does not leave a human state, and that its guard holds. Budgets follow
graph.yaml: an edge's budget is consumed each time the edge is taken, no matter
how many model invocations the target stage makes (ADR 0003, decision 4).
Production guard evaluation belongs to the orchestrator; the shipped default
denies everything.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from agentic_pipeline.graph import Edge, Graph

type GuardEvaluator = Callable[[Edge, Mapping[str, Any]], bool]


class TransitionDenied(Exception):
    pass


class BudgetExhausted(TransitionDenied):
    pass


def deny_all_guards(edge: Edge, run_context: Mapping[str, Any]) -> bool:
    return False


def check_transition(
    graph: Graph,
    src: str,
    guard: str,
    to: str,
    evaluator: GuardEvaluator,
    run_context: Mapping[str, Any],
) -> Edge:
    edge = next((e for e in graph.edges if (e.src, e.guard, e.to) == (src, guard, to)), None)
    if edge is None:
        raise TransitionDenied(f"no edge {src} --{guard}--> {to} in pipeline/graph.yaml")
    if src in graph.human_states:
        raise TransitionDenied(f"leaving human state {src} requires a recorded human decision")
    if not evaluator(edge, run_context):
        raise TransitionDenied(f"guard {guard!r} on {src} -> {to} not satisfied")
    return edge


class ReservationState(StrEnum):
    RESERVED = "reserved"
    COMMITTED = "committed"
    RELEASED = "released"


@dataclass(slots=True)
class Reservation:
    ledger: BudgetLedger
    budget: str
    state: ReservationState = ReservationState.RESERVED

    def commit(self) -> None:
        self._settle(ReservationState.COMMITTED)
        self.ledger.consumed[self.budget] = self.ledger.consumed.get(self.budget, 0) + 1

    def release(self) -> None:
        self._settle(ReservationState.RELEASED)

    def _settle(self, new: ReservationState) -> None:
        if self.state is not ReservationState.RESERVED:
            raise RuntimeError(f"reservation for {self.budget!r} already {self.state}")
        self.state = new
        self.ledger.reserved[self.budget] -= 1


@dataclass(slots=True)
class BudgetLedger:
    """Per-run budget counts. `max_retries` is the number of times budgeted edges may be taken."""

    limits: dict[str, int]
    consumed: dict[str, int] = field(default_factory=dict)
    reserved: dict[str, int] = field(default_factory=dict)

    @classmethod
    def from_policy(cls, budgets: Mapping[str, Any]) -> BudgetLedger:
        return cls({k: v["max_retries"] for k, v in (budgets.get("budgets") or {}).items()})

    def reserve(self, edge: Edge) -> Reservation | None:
        """Reserve one unit for `edge`; None if the edge carries no budget."""
        if edge.budget is None:
            return None
        if edge.budget not in self.limits:
            raise BudgetExhausted(f"unknown budget {edge.budget!r}")
        used = self.consumed.get(edge.budget, 0) + self.reserved.get(edge.budget, 0)
        if used >= self.limits[edge.budget]:
            raise BudgetExhausted(f"budget {edge.budget!r} exhausted ({used} used)")
        self.reserved[edge.budget] = self.reserved.get(edge.budget, 0) + 1
        return Reservation(self, edge.budget)
