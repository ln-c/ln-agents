"""Static invariants for policy/model_registry.yaml, cross-checked against the graph.

Every error is prefixed with its rule ID:

R1   registry validates against schemas/model_registry.schema.json
R2   referenced repo paths exist and stay inside the repo root
R3   agent backend references name a declared model, or a non-model provider
R4   stage_requirements covers exactly the graph states executed by a model
R5   each operation's dotted `agent` resolves to an agents entry
R6   operation backends, derived from the agent's primary and model_assistance, are models
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
R17  every model provider declares family patterns; every model names a model provider, its
     model_id matches a pattern, and its declared family agrees; (provider, model_id) are unique;
     only a model whose provider's adapter passes it on declares an `effort`
R18  every model an operation may run on declares the capabilities the operation requires
R19  a provider the dispatcher may start (`launch`) is local and binds exactly the loopback
     host and port its readiness probe checks
R20  every agent spec has a model: each `agents/*.md` is the `spec` of exactly one agents entry,
     and an entry with a `spec` names at least one catalog model in a backend slot
R21  every agent spec in the repository has one short brief (between the markers in
     `briefs.py`), which is what a coordinator reads in place of the file; a run's frozen copy is
     not held to it, so a run started before briefs existed keeps going
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Mapping
from pathlib import Path, PurePosixPath
from typing import Any

from agentic_pipeline.briefs import BriefError
from agentic_pipeline.briefs import extract as extract_brief
from agentic_pipeline.graph import Graph
from agentic_pipeline.schemas import ValidationError, validator

MODEL_ACCESS_MODES = frozenset({"subscription_tool", "local_inference", "free_allowance_only"})
EFFORT_PROVIDERS = frozenset({"claude_pro"})  # ClaudeAdapter passes `effort` as claude --effort
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


def agent_spec_path(registry: Mapping[str, Any], dotted: str) -> str | None:
    """The spec file of the agent an operation's dotted name belongs to (`design.proposal_a`
    belongs to `design`), or None if that agent has none."""
    agent = registry["agents"].get(dotted.split(".")[0])
    spec = agent.get("spec") if isinstance(agent, Mapping) else None
    return PurePosixPath(spec).as_posix() if isinstance(spec, str) else None


def pattern_family(provider: Mapping[str, Any], model_id: str) -> str | None:
    """The family a provider's `family_patterns` assign to a model id (first match), else None.

    Patterns use `re.match`, so each is anchored at the start of the id. A bad pattern is None.
    """
    for rule in provider.get("family_patterns") or ():
        try:
            if re.match(rule["pattern"], model_id):
                return str(rule["family"])
        except re.error:
            return None
    return None


def backend_model(registry: Mapping[str, Any], backend: str) -> Mapping[str, Any] | None:
    """The catalog entry a backend id names, or None (it may be a non-model provider)."""
    return (registry.get("models") or {}).get(backend)


def backend_provider(registry: Mapping[str, Any], backend: str) -> Mapping[str, Any] | None:
    """The provider entry behind a backend: a model's provider, or the provider itself."""
    model = backend_model(registry, backend)
    return registry["providers"].get(model["provider"] if model else backend)


def backend_location(registry: Mapping[str, Any], backend: str) -> str | None:
    provider = backend_provider(registry, backend)
    return provider.get("location") if provider else None


def backend_family(registry: Mapping[str, Any], backend: str) -> str | None:
    model = backend_model(registry, backend)
    return str(model["family"]) if model else None


def backend_capabilities(registry: Mapping[str, Any], backend: str) -> frozenset[str]:
    model = backend_model(registry, backend)
    return frozenset(model["capabilities"]) if model else frozenset()


def operations(stage_reqs: Mapping[str, Any]) -> Iterator[tuple[str, Op]]:
    for stage, spec in stage_reqs.items():
        for op in spec.get("operations") or ():
            yield stage, op


def op_backends(registry: Mapping[str, Any], stage: str, op: Op) -> list[str]:
    """Models an operation may run on, in preference order.

    The agent's entry in `agents` is the only place a model is bound; stage_requirements names
    none. `all_of` runs on the agent's `primary`. `ordered_any_of` tries the `primary` (when it
    is a model; synthesis, for one, is owned by the orchestrator) and then each
    `model_assistance` entry, without repeats. A non-model entry is kept so R6 reports it.
    """
    agent = resolve_agent(registry["agents"], op["agent"]) or {}
    primary = agent.get("primary")
    if registry["stage_requirements"][stage]["mode"] != "ordered_any_of":
        return [primary] if primary else []
    first = [primary] if backend_model(registry, primary) is not None else []
    return list(dict.fromkeys([*first, *(agent.get("model_assistance") or ())]))


def _flag(argv: list[str], flag: str) -> list[str]:
    """Every value given to `flag` in argv, as `--flag value` or `--flag=value`."""
    values = [argv[i + 1] for i, a in enumerate(argv[:-1]) if a == flag]
    return values + [a.split("=", 1)[1] for a in argv if a.startswith(f"{flag}=")]


def launch_errors(provider: Mapping[str, Any]) -> list[str]:
    """Why a provider's `launch` could start a server other than the one readiness checks."""
    if provider.get("location") != "local" or "protocol" not in (provider.get("readiness") or {}):
        return ["`launch` is only for a local inference server with a readiness probe"]
    argv, spec = provider["launch"]["argv"], provider["readiness"]
    errors = []
    if _flag(argv, "--host") != [spec["host"]]:
        errors.append(f"launch must pass --host {spec['host']} exactly once")
    if _flag(argv, "--port") != [str(spec["port"])]:
        errors.append(f"launch must pass --port {spec['port']} exactly once")
    return errors


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


def validate_registry(
    registry: Mapping[str, Any], graph: Graph, root: Path, policy_root: Path | None = None
) -> list[str]:
    """Return a list of invariant violations (empty means valid).

    `root` holds the schemas. Repository paths (R2) and the agent specs (R20) are looked for in
    `policy_root` when it holds an `agents/` folder (a run's frozen copy), else in `root`; the
    briefs (R21) are checked only in `root`'s own specs.
    """
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

    # R19. A server the dispatcher starts listens where readiness looks, on loopback only.
    for name, provider in providers.items():
        if "launch" in provider:
            errors.extend(f"registry R19 providers.{name}: {e}" for e in launch_errors(provider))

    # R17. Models name a model provider, agree with its family patterns, and are unique.
    served: dict[tuple[str, str], str] = {}
    for name, provider in providers.items():
        if provider["access_mode"] in MODEL_ACCESS_MODES and not provider.get("family_patterns"):
            errors.append(
                f"registry R17 providers.{name}: a model provider declares family_patterns"
            )
        for rule in provider.get("family_patterns") or ():
            try:
                re.compile(rule["pattern"])
            except re.error as exc:
                errors.append(
                    f"registry R17 providers.{name}: bad pattern {rule['pattern']!r}: {exc}"
                )
    for name, model in registry["models"].items():
        provider = providers.get(model["provider"])
        if provider is None or provider["access_mode"] not in MODEL_ACCESS_MODES:
            errors.append(
                f"registry R17 models.{name}: {model['provider']!r} is not a model provider"
            )
            continue
        key = (model["provider"], model["model_id"])
        if key in served:
            errors.append(
                f"registry R17 models.{name}: same provider and model_id as {served[key]}"
            )
        served.setdefault(key, name)
        if "effort" in model and model["provider"] not in EFFORT_PROVIDERS:
            errors.append(
                f"registry R17 models.{name}: `effort` is only passed to {sorted(EFFORT_PROVIDERS)}"
            )
        matched = pattern_family(provider, model["model_id"])
        if matched is None:
            errors.append(
                f"registry R17 models.{name}: model_id {model['model_id']!r} matches no "
                f"family pattern of {model['provider']}"
            )
        elif matched != model["family"]:
            errors.append(
                f"registry R17 models.{name}: declares family {model['family']!r} but its "
                f"model_id matches {matched!r}"
            )

    # R2. Repo paths exist and do not escape the root.
    base = policy_root if policy_root is not None and (policy_root / "agents").is_dir() else root
    repo = base.resolve()
    for where, rel in _repo_paths(registry):
        path = (repo / rel).resolve()
        if not path.is_relative_to(repo):
            errors.append(f"registry R2 {where}: {rel!r} escapes the repository root")
        elif not path.is_file():
            errors.append(f"registry R2 {where}: {rel!r} does not exist")

    # R20. Every agent spec has a model: one owner per spec file, and a model it may run on.
    models = registry["models"]
    owners: dict[str, str] = {}
    for name, agent in agents.items():
        spec = agent_spec_path(registry, name)
        if spec is None:
            continue
        if spec in owners:
            errors.append(
                f"registry R20 agents.{name}: {spec!r} is also agents.{owners[spec]}'s spec"
            )
        owners.setdefault(spec, name)
        if not any(ref in models for _, ref in _backend_refs(agent, f"agents.{name}")):
            errors.append(f"registry R20 agents.{name}: its spec {spec!r} names no catalog model")
    for path in sorted((repo / "agents").glob("*.md")):
        if f"agents/{path.name}" not in owners:
            errors.append(f"registry R20: agents/{path.name} is not the spec of any agents entry")

    # R21. Every spec carries the brief a coordinator reads in its place. It guards what a new
    # run would freeze: a run's frozen copy keeps the specs it started with, briefed or not.
    for spec, name in owners.items():
        path = repo / spec
        if base is not root or not path.is_file():
            continue  # a frozen copy, or a spec R2 has already reported missing
        try:
            extract_brief(path.read_text(encoding="utf-8"))
        except (BriefError, UnicodeDecodeError) as exc:
            errors.append(f"registry R21 agents.{name}: {spec!r} {exc}")

    # R3. Backend references name a model, or a provider that is not a model provider.
    for where, ref in _backend_refs(agents, "agents"):
        if ref in models:
            continue
        if ref not in providers:
            errors.append(f"registry R3 {where}: unknown model {ref!r}")
        elif providers[ref]["access_mode"] in MODEL_ACCESS_MODES:
            errors.append(f"registry R3 {where}: {ref!r} is a provider; bind one of its models")

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

        # R6. Backends are models.
        backends = op_backends(registry, stage, op)
        if not backends:
            errors.append(f"registry R6 {tag}: no backend resolved")
        for backend in backends:
            if backend not in models:
                errors.append(f"registry R6 {tag}: {backend!r} is not a model")

        # R18. Declared capabilities cover the operation's requirements.
        for backend in backends:
            if missing := sorted(set(op["requires"]) - backend_capabilities(registry, backend)):
                errors.append(f"registry R18 {tag}: {backend!r} does not declare {missing}")

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
    # A backend with no model entry is R3/R6's error, not a second one here.
    for stage, spec in stage_reqs.items():
        if spec["mode"] != "all_of":
            continue
        seen: dict[str, str] = {}
        for op in spec["operations"]:
            if "session_separation" not in profiles.get(op["isolation_profile"], ()):
                continue
            for backend in op_backends(registry, stage, op):
                family = backend_family(registry, backend)
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
