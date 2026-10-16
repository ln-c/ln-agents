"""Static invariants for policy/model_registry.yaml, cross-checked against the graph.

Every error is prefixed with its rule ID:

R1   registry validates against schemas/model_registry.schema.json
R2   referenced repo paths exist and stay inside the repo root
R3   agent backend references name declared providers
R4   stage_requirements covers exactly the graph states executed by a model
R5   each operation's dotted `agent` resolves to an agents entry
R6   operation backends are model providers permitted for that agent
R7   reads and targets resolve to the artifact catalog
R8   allowed_reads and denied_reads are disjoint
R9   target_artifact is sealed and readable
R10  unsealed artifacts are readable only by the operation that produces them
R11  an operation never reads output of another operation in the same stage
R12  write roots are declared, unshared, distinct, and non-nested
R13  isolation profiles are declared; artifact producers name real states/operations
R14  isolated operations use distinct model families; no critic reviews its own agent's work
R15  an operation's output_schema names a schema that exists in schemas/
R16  every provider declares where its traffic goes, and it matches its access mode
R17  every model provider resolves to exactly one model family
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping
from pathlib import Path, PurePosixPath
from typing import Any

from agentic_pipeline.graph import Graph
from agentic_pipeline.schemas import ValidationError, validator

MODEL_ACCESS_MODES = frozenset({"subscription_tool", "local_inference", "free_allowance_only"})
# Where each provider access mode's traffic goes. A mode not listed is rejected (R16), and
# dispatch treats anything that is not `local` as leaving the host.
ACCESS_MODE_LOCATION: Mapping[str, str] = {
    "subscription_tool": "cloud",
    "free_allowance_only": "cloud",
    "local_inference": "local",
    "local_code": "local",
    "local_or_ci_tools": "local",
    "repository_runtime": "local",
    "local_code_and_storage": "local",
    "human_interface_and_repository_runtime": "local",
}
BACKEND_KEYS = frozenset(
    {
        "primary",
        "escalation",
        "optional_classifier",
        "explanation_fallback",
        "cloud_explanation_fallback",
        "analysis_fallback",
        "subscription_analysis",
        "model_assistance",
    }
)
EXECUTION_PATH_KEYS = (
    "state_graph",
    "retry_budget_owner",
    "hard_veto_policy",
    "data_classification_policy",
    "escalation_policy",
)

type Op = Mapping[str, Any]


def resolve_agent(agents: Mapping[str, Any], dotted: str) -> Mapping[str, Any] | None:
    node: Any = agents
    for part in dotted.split("."):
        if not isinstance(node, Mapping) or part not in node:
            return None
        node = node[part]
    return node if isinstance(node, Mapping) else None


def resolve_family(provider: Mapping[str, Any]) -> str | None:
    """The model family a provider serves, or None when it cannot be determined.

    A fixed `family` wins. Otherwise the provider's pinned model id is matched against the
    ordered `family_by_model_id` patterns (`re.match`, so a pattern is anchored at the start);
    the first match decides. A bad pattern, a missing model id or no match is None.
    """
    if "family" in provider:
        return str(provider["family"])
    model_id = provider.get("model_id") or (provider.get("readiness") or {}).get("model_id")
    if not isinstance(model_id, str):
        return None
    for rule in provider.get("family_by_model_id") or ():
        try:
            if re.match(rule["pattern"], model_id):
                return str(rule["family"])
        except re.error:
            return None
    return None


def operations(stage_reqs: Mapping[str, Any]) -> Iterator[tuple[str, Op]]:
    for stage, spec in stage_reqs.items():
        for op in spec.get("operations") or ():
            yield stage, op


def op_backends(registry: Mapping[str, Any], stage: str, op: Op) -> list[str]:
    spec = registry["stage_requirements"][stage]
    if spec["mode"] == "ordered_any_of":
        return list(spec["backends"])
    agent = resolve_agent(registry["agents"], op["agent"]) or {}
    return [agent["primary"]] if "primary" in agent else []


def _repo_paths(registry: Mapping[str, Any]) -> Iterator[tuple[str, str]]:
    execution, agents = registry["execution"], registry["agents"]
    for key in EXECUTION_PATH_KEYS:
        yield f"execution.{key}", execution[key]
    for name, agent in agents.items():
        if "spec" in agent:
            yield f"agents.{name}.spec", agent["spec"]
    for i, path in enumerate(agents["scoring_authorization"].get("policy_inputs") or ()):
        yield f"agents.scoring_authorization.policy_inputs[{i}]", path
    for key in ("routing_policy", "retry_budget_policy"):
        if key in agents["exception_router"]:
            yield f"agents.exception_router.{key}", agents["exception_router"][key]


def _backend_refs(node: Mapping[str, Any], prefix: str) -> Iterator[tuple[str, str]]:
    for key, value in node.items():
        where = f"{prefix}.{key}"
        if isinstance(value, Mapping):
            yield from _backend_refs(value, where)
        elif key in BACKEND_KEYS:
            for ref in value if isinstance(value, list) else [value]:
                yield where, ref


def validate_registry(registry: Mapping[str, Any], graph: Graph, root: Path) -> list[str]:
    """Return a list of invariant violations (empty means valid)."""
    try:
        validator("model_registry", root / "schemas")(registry)
    except ValidationError as exc:
        return [f"registry R1: schema violation: {exc.message}"]

    errors: list[str] = []
    providers = registry["providers"]
    agents = registry["agents"]
    artifacts = registry["artifacts"]
    roots = registry["workspace_roots"]
    profiles = registry["isolation_profiles"]
    stage_reqs = registry["stage_requirements"]
    ops = list(operations(stage_reqs))
    op_index = {f"{stage}.{op['id']}": op for stage, op in ops}

    # R16/R17. Provider location and model family.
    for name, provider in providers.items():
        mode = provider["access_mode"]
        expected = ACCESS_MODE_LOCATION.get(mode)
        if expected is None:
            errors.append(f"registry R16 providers.{name}: unknown access mode {mode!r}")
        elif provider["location"] != expected:
            errors.append(
                f"registry R16 providers.{name}: access mode {mode!r} is {expected}, "
                f"declared {provider['location']!r}"
            )
        if mode not in MODEL_ACCESS_MODES:
            continue
        if ("family" in provider) == ("family_by_model_id" in provider):
            errors.append(
                f"registry R17 providers.{name}: declare exactly one of family, family_by_model_id"
            )
        else:
            bad = False
            for rule in provider.get("family_by_model_id") or ():
                try:
                    re.compile(rule["pattern"])
                except re.error as exc:
                    bad = True
                    errors.append(
                        f"registry R17 providers.{name}: bad pattern {rule['pattern']!r}: {exc}"
                    )
            if not bad and resolve_family(provider) is None:
                errors.append(f"registry R17 providers.{name}: model id matches no family pattern")

    # R2. Repo paths exist and do not escape the root.
    repo = root.resolve()
    for where, rel in _repo_paths(registry):
        path = (repo / rel).resolve()
        if not path.is_relative_to(repo):
            errors.append(f"registry R2 {where}: {rel!r} escapes the repository root")
        elif not path.is_file():
            errors.append(f"registry R2 {where}: {rel!r} does not exist")

    # R3. Backend references name declared providers.
    for where, ref in _backend_refs(agents, "agents"):
        if ref not in providers:
            errors.append(f"registry R3 {where}: unknown provider {ref!r}")

    # R4. Coverage matches the graph's model-executed states.
    model_states = {s for s, spec in graph.states.items() if "model" in spec.get("executor", "")}
    for stage in sorted(model_states - stage_reqs.keys()):
        errors.append(f"registry R4 {stage}: model state missing from stage_requirements")
    for stage in sorted(stage_reqs.keys() - model_states):
        errors.append(f"registry R4 {stage}: not a model-executed graph state")

    for stage, op in ops:
        tag = f"{stage}.{op['id']}"

        # R5. Agent resolves.
        agent = resolve_agent(agents, op["agent"])
        if agent is None:
            errors.append(f"registry R5 {tag}: unknown agent {op['agent']!r}")
            continue

        # R6. Backends are model providers permitted for the agent.
        permitted = {agent.get("primary"), *(agent.get("model_assistance") or ())}
        backends = op_backends(registry, stage, op)
        if not backends:
            errors.append(f"registry R6 {tag}: no backend resolved")
        for backend in backends:
            mode = providers.get(backend, {}).get("access_mode")
            if mode not in MODEL_ACCESS_MODES:
                errors.append(f"registry R6 {tag}: {backend!r} is not a model provider")
            elif stage_reqs[stage]["mode"] == "ordered_any_of" and backend not in permitted:
                errors.append(f"registry R6 {tag}: {backend!r} not permitted for {op['agent']}")

        # R7. Artifact references resolve.
        allowed, denied = set(op["allowed_reads"]), set(op["denied_reads"])
        target = op.get("target_artifact")
        for ref in sorted(allowed | denied | ({target} if target else set())):
            if ref not in artifacts:
                errors.append(f"registry R7 {tag}: unknown artifact {ref!r}")

        # R8. Allowed and denied reads are disjoint.
        if overlap := allowed & denied:
            errors.append(f"registry R8 {tag}: both allowed and denied: {sorted(overlap)}")

        # R9. Targets are sealed and readable.
        if target in artifacts:
            if not artifacts[target]["sealed"]:
                errors.append(f"registry R9 {tag}: target {target!r} is not sealed")
            if target not in allowed:
                errors.append(f"registry R9 {tag}: target {target!r} not in allowed_reads")

        # R10/R11. Unsealed and same-stage reads.
        for ref in sorted(allowed & artifacts.keys()):
            producers = artifacts[ref]["producer"]
            if not artifacts[ref]["sealed"] and tag not in producers:
                errors.append(f"registry R10 {tag}: reads unsealed artifact {ref!r}")
            if any(p.startswith(f"{stage}.") and p != tag for p in producers):
                errors.append(f"registry R11 {tag}: reads same-stage output {ref!r}")

        # R13 (operation part). Isolation profile declared.
        if op["isolation_profile"] not in profiles:
            errors.append(
                f"registry R13 {tag}: unknown isolation profile {op['isolation_profile']!r}"
            )

    # R12. Write roots.
    owners: dict[str, str] = {}
    for stage, op in ops:
        key, op_id = op["write_root"], f"{stage}.{op['id']}"
        if key not in roots:
            errors.append(f"registry R12 {op_id}: unknown write root {key!r}")
        elif key in owners:
            errors.append(f"registry R12 {op_id}: write root {key!r} shared with {owners[key]}")
        else:
            owners[key] = op_id
    paths = {name: PurePosixPath(p) for name, p in roots.items()}
    names = sorted(paths)
    for i, a in enumerate(names):
        for b in names[i + 1 :]:
            pa, pb = paths[a], paths[b]
            if pa == pb or pa.is_relative_to(pb) or pb.is_relative_to(pa):
                errors.append(f"registry R12: workspace roots {a!r} and {b!r} overlap")

    # R13 (artifact part). Producers name real states and operations.
    for name, spec in artifacts.items():
        for producer in spec["producer"]:
            state, _, op_id = producer.partition(".")
            if state not in graph.states:
                errors.append(f"registry R13 artifact {name}: unknown producer state {state!r}")
            elif op_id and producer not in op_index:
                errors.append(f"registry R13 artifact {name}: unknown producer {producer!r}")

    # R15. Output schemas exist.
    for stage, spec in stage_reqs.items():
        for op in spec.get("operations", []):  # a disabled stage has none
            name = op.get("output_schema")
            if name is not None and not (root / "schemas" / f"{name}.schema.json").is_file():
                errors.append(
                    f"registry R15 {stage}.{op['id']}: output_schema {name!r} has no schema file"
                )

    # R14. Independence: distinct model families under session separation; no self-critique.
    # A family that cannot be resolved is R17's error, not a second one here.
    for stage, spec in stage_reqs.items():
        if spec["mode"] != "all_of":
            continue
        seen: dict[str, str] = {}
        for op in spec["operations"]:
            if "session_separation" not in profiles.get(op["isolation_profile"], ()):
                continue
            for backend in op_backends(registry, stage, op):
                family = resolve_family(providers.get(backend, {}))
                if family is None:
                    continue
                if family in seen:
                    errors.append(
                        f"registry R14 {stage}.{op['id']}: backend {backend!r} shares "
                        f"model family {family!r} with {stage}.{seen[family]}"
                    )
                seen.setdefault(family, op["id"])
    for stage, op in ops:
        target = op.get("target_artifact")
        for producer in artifacts.get(target, {}).get("producer", ()) if target else ():
            author = op_index.get(producer)
            if author is not None and author["agent"] == op["agent"]:
                errors.append(
                    f"registry R14 {stage}.{op['id']}: {op['agent']} critiques its own "
                    f"artifact {target!r}"
                )

    return errors
