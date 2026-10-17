"""Stage interfaces (stubs). Each phase of pipeline/graph.yaml gets a Protocol.

Implementations are out of scope for the skeleton; these exist so agents have a
typed target and so invariants (who may do what) are visible in code.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

type Artifact = dict[str, Any]


@dataclass(frozen=True, slots=True)
class Transition:
    """Result of executing a state: the guard that fired and produced artifacts."""

    guard: str
    artifacts: tuple[Artifact, ...] = ()


class Grounding(Protocol):
    """G: pin snapshot, classify, index, write immutable manifest."""

    def run(self, task: Artifact) -> Transition: ...


class Designer(Protocol):
    """A1-A3: independent proposals, cross-critique, synthesis. Never sees weights."""

    def propose(self, manifest: Artifact) -> Transition: ...
    def critique(self, proposals: list[Artifact]) -> Transition: ...
    def synthesize(self, proposals: list[Artifact]) -> Transition: ...


class Validator(Protocol):
    """A4 and B1: deterministic code only. Model output can never clear a veto."""

    def validate(self, candidates: list[Artifact]) -> Transition: ...
    def veto(self, candidates: list[Artifact]) -> Transition: ...


class Scorer(Protocol):
    """B2: typed per-criterion values; weights come from policy/weights.yaml only."""

    def score(self, survivors: list[Artifact], weights: Artifact) -> Transition: ...


class Builder(Protocol):
    """C1, D1, E1/E4: contracts, tests, implementation. E1 writes implementation files only."""

    def decompose(self, decision: Artifact) -> Transition: ...
    def write_tests(self, contracts: Artifact) -> Transition: ...
    def implement(self, spec: Artifact, sandbox: Artifact) -> Transition: ...


class Checker(Protocol):
    """E2 and F1: deterministic checks; protected-test hashes checked before and after."""

    def check(self, patch: Artifact) -> Transition: ...


class Router(Protocol):
    """X: reproduce, classify, count budget, route. Cannot mutate protected artifacts."""

    def route(self, payload: Artifact) -> Transition: ...


class ProvenanceLog(Protocol):
    """L1: append-only; events written before each external model call."""

    def append(self, event: Artifact) -> None: ...


def not_implemented(stage: str) -> Transition:
    raise NotImplementedError(f"stage {stage} is not implemented in the skeleton")
