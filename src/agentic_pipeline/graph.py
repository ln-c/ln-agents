"""Load the pipeline state machine and enforce its structural invariants.

Pure stdlib graph algorithms; the graph is small (~45 states), so clarity wins.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

try:  # libyaml C loader is much faster; fall back to pure Python if unavailable.
    _Loader: type = yaml.CSafeLoader
except AttributeError:  # pragma: no cover
    _Loader = yaml.SafeLoader

REPO_ROOT = Path(__file__).resolve().parents[2]
NO_EDGE_KINDS = frozenset({"terminal", "sink"})
KINDS = frozenset({"code", "model", "human", "decision", "terminal", "sink"})
ROUTER = "X"
ROUTER_FORBIDDEN = frozenset({"contracts", "protected_tests", "policy", "weights", "budgets"})


def load_yaml(path: Path) -> Any:
    return loads_yaml(path.read_bytes())


def loads_yaml(data: bytes) -> Any:
    return yaml.load(data, Loader=_Loader)  # noqa: S506 - safe loader subclass


@dataclass(frozen=True, slots=True)
class Edge:
    src: str
    to: str
    guard: str
    budget: str | None = None


@dataclass(slots=True)
class Graph:
    states: dict[str, dict[str, Any]]
    edges: list[Edge]
    entrypoints: list[str]
    human_states: frozenset[str]
    guarded_paths: list[dict[str, Any]] = field(default_factory=list)
    # state -> [(guard, side-effect state)]: run once when the state leaves by that guard
    side_effects: dict[str, list[tuple[str, str]]] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, raw: Mapping[str, Any]) -> Graph:
        states: dict[str, dict[str, Any]] = dict(raw.get("states") or {})
        edges = [
            Edge(name, e["to"], e.get("guard", ""), e.get("budget"))
            for name, spec in states.items()
            for e in (spec.get("edges") or [])
        ]
        return cls(
            states=states,
            edges=edges,
            entrypoints=list(raw.get("entrypoints") or []),
            human_states=frozenset(raw.get("human_states") or []),
            guarded_paths=list(raw.get("guarded_paths") or []),
            side_effects={
                name: [(str(se.get("when", "")), str(se.get("run", ""))) for se in effects]
                for name, spec in states.items()
                if (effects := spec.get("side_effects"))
            },
        )

    @classmethod
    def load(cls, path: Path | None = None) -> Graph:
        return cls.from_mapping(load_yaml(path or REPO_ROOT / "pipeline" / "graph.yaml"))

    def successors(self, node: str) -> Iterable[str]:
        """Edge targets, plus side-effect states (they run, so they must count as reached)."""
        yield from (e.to for e in self.edges if e.src == node)
        yield from (run for _, run in self.side_effects.get(node, []))

    def reachable(self, starts: Iterable[str], removed: frozenset[str] = frozenset()) -> set[str]:
        seen: set[str] = set()
        queue = deque(s for s in starts if s not in removed)
        while queue:
            node = queue.popleft()
            if node in seen:
                continue
            seen.add(node)
            queue.extend(n for n in self.successors(node) if n not in removed and n not in seen)
        return seen


def _find_cycle(nodes: Iterable[str], adj: Mapping[str, list[str]]) -> list[str] | None:
    """Return one cycle in the directed graph, or None (iterative DFS, 3-colour)."""
    white, grey, black = 0, 1, 2
    colour = dict.fromkeys(nodes, white)
    for root in colour:
        if colour[root] != white:
            continue
        stack: list[tuple[str, Iterable[str]]] = [(root, iter(adj.get(root, ())))]
        path = [root]
        colour[root] = grey
        while stack:
            node, it = stack[-1]
            nxt = next(it, None)
            if nxt is None:
                colour[node] = black
                stack.pop()
                path.pop()
            elif colour.get(nxt) == grey:
                return [*path[path.index(nxt) :], nxt]
            elif colour.get(nxt) == white:
                colour[nxt] = grey
                stack.append((nxt, iter(adj.get(nxt, ()))))
                path.append(nxt)
    return None


def spent_route(graph: Graph, src: str, target: str) -> Edge | None:
    """The edge taken from `src` once a budget whose on_exhausted is `target` is spent.

    The single unbudgeted edge from `src` to `target`; if there are several, the single
    one whose guard ends in `_spent`. None when no unambiguous route exists.
    """
    routes = [e for e in graph.edges if e.src == src and e.to == target and e.budget is None]
    if len(routes) > 1:
        routes = [e for e in routes if e.guard.endswith("_spent")]
    return routes[0] if len(routes) == 1 else None


def validate(
    graph: Graph,
    budgets: Mapping[str, Any],
    routing: Mapping[str, Any] | None = None,
) -> list[str]:
    """Return a list of invariant violations (empty means valid)."""
    errors: list[str] = []
    names = set(graph.states)

    # 1. Well-formedness: kinds, edge targets, outgoing edges, reachability.
    for name, spec in graph.states.items():
        kind = spec.get("kind")
        if kind not in KINDS:
            errors.append(f"{name}: unknown kind {kind!r}")
        has_edges = bool(spec.get("edges"))
        if kind in NO_EDGE_KINDS and has_edges:
            errors.append(f"{name}: {kind} state must not have outgoing edges")
        if kind not in NO_EDGE_KINDS and not has_edges:
            errors.append(f"{name}: non-terminal state has no outgoing edges")
    for e in graph.edges:
        if e.to not in names:
            errors.append(f"{e.src} -> {e.to}: edge target does not exist")
    for ep in graph.entrypoints:
        if ep not in names:
            errors.append(f"entrypoint {ep} does not exist")
    unreachable = names - graph.reachable(graph.entrypoints)
    if unreachable:
        errors.append(f"unreachable states: {sorted(unreachable)}")

    # 2. Every cycle must consume a budget or pass through a human state.
    adj: dict[str, list[str]] = {}
    for e in graph.edges:
        if e.budget is None and e.src not in graph.human_states and e.to not in graph.human_states:
            adj.setdefault(e.src, []).append(e.to)
    if cycle := _find_cycle(names - graph.human_states, adj):
        errors.append(f"unbounded loop (no budget, no human gate): {' -> '.join(cycle)}")

    # 3. Every referenced budget exists in policy/budgets.yaml.
    known = set((budgets.get("budgets") or {}).keys())
    for e in graph.edges:
        if e.budget is not None and e.budget not in known:
            errors.append(f"{e.src} -> {e.to}: unknown budget {e.budget!r}")
    for key, spec in (budgets.get("budgets") or {}).items():
        target = (spec or {}).get("on_exhausted")
        if target not in names:
            errors.append(f"budget {key}: on_exhausted target {target!r} does not exist")

    # 4. Every terminal state writes provenance (L1).
    for name, spec in graph.states.items():
        if spec.get("kind") == "terminal" and "L1" not in (spec.get("writes") or []):
            errors.append(f"{name}: terminal state must write provenance to L1")

    # 5. Guarded paths: removing any `through` group must cut the target off.
    for gp in graph.guarded_paths:
        target = gp["target"]
        for group in gp["through"]:
            if target in graph.reachable(graph.entrypoints, frozenset(group)):
                errors.append(f"{target} reachable while bypassing {group}")

    # 6. Human states are executed by humans only.
    for name in graph.human_states:
        spec = graph.states.get(name, {})
        if spec.get("kind") != "human" or spec.get("executor") != "human":
            errors.append(f"{name}: human state must have kind=human and executor=human")
    for name, spec in graph.states.items():
        if spec.get("kind") == "human" and name not in graph.human_states:
            errors.append(f"{name}: kind=human but not listed in human_states")

    # 7. The router cannot mutate protected artifacts.
    router = graph.states.get(ROUTER, {})
    if router.get("may_modify") != []:
        errors.append("X: router must declare may_modify: []")
    if not ROUTER_FORBIDDEN <= set(router.get("forbidden_mutations") or []):
        errors.append(f"X: forbidden_mutations must include {sorted(ROUTER_FORBIDDEN)}")

    # 8. Routing policy and router edges agree.
    if routing is not None:
        policy = {(k, v["to"], v.get("budget")) for k, v in (routing.get("routes") or {}).items()}
        edges = {(e.guard, e.to, e.budget) for e in graph.edges if e.src == ROUTER}
        for item in sorted(policy ^ edges, key=str):
            side = "policy/routing.yaml" if item in policy else "graph X edges"
            errors.append(f"router mismatch, only in {side}: {item}")

    # 9. Every budgeted retry has a way out: when the budget is spent, its source has
    #    exactly one route to the budget's on_exhausted target (see spent_route), which
    #    the runner takes itself. Human states are exempt (a human chooses).
    exhausted_targets = {
        key: (spec or {}).get("on_exhausted")
        for key, spec in (budgets.get("budgets") or {}).items()
    }
    for e in graph.edges:
        if e.budget is None or e.src in graph.human_states:
            continue
        target = exhausted_targets.get(e.budget)
        if target is None or target == e.src:
            continue
        if spent_route(graph, e.src, target) is None:
            errors.append(
                f"{e.src}: budget {e.budget!r} has no spent route to {target} "
                f"(its on_exhausted target): need one unbudgeted edge, or exactly one "
                f"whose guard ends in '_spent'"
            )

    # 10. Side effects run inside their state's step and never take control: each names a
    #     guard the state really has and a code state whose edges lead only to sinks.
    for name, effects in graph.side_effects.items():
        guards = {e.guard for e in graph.edges if e.src == name}
        for on, run in effects:
            if on not in guards:
                errors.append(f"{name}: side effect on unknown guard {on!r}")
            target = graph.states.get(run)
            if target is None:
                errors.append(f"{name}: side effect state {run!r} does not exist")
                continue
            if target.get("kind") != "code" or run in graph.human_states:
                errors.append(f"{name}: side effect {run} must be a code state")
            for e in graph.edges:
                if e.src == run and graph.states.get(e.to, {}).get("kind") != "sink":
                    errors.append(f"{name}: side effect {run} must lead only to sinks, not {e.to}")
            if any(e.to == run for e in graph.edges):
                errors.append(f"{name}: side effect {run} must not also be entered by an edge")

    return errors


def validate_repo(root: Path = REPO_ROOT) -> list[str]:
    graph = Graph.load(root / "pipeline" / "graph.yaml")
    budgets = load_yaml(root / "policy" / "budgets.yaml")
    routing = load_yaml(root / "policy" / "routing.yaml")
    return validate(graph, budgets, routing)
