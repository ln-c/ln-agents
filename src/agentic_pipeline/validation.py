"""Static repository validation: graph, policies, and model registry."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml

from agentic_pipeline.graph import REPO_ROOT, Graph, load_yaml, validate
from agentic_pipeline.policies import POLICY_FILES, check_artifact_defaults, validate_policies
from agentic_pipeline.registry import validate_registry


def _load(root: Path, rel: str, errors: list[str]) -> Any:
    try:
        doc = load_yaml(root / rel)
    except FileNotFoundError:
        errors.append(f"missing file: {rel}")
        return None
    except yaml.YAMLError as exc:
        errors.append(f"unparseable YAML: {rel}: {exc}")
        return None
    if not isinstance(doc, Mapping):
        errors.append(f"{rel}: top level must be a mapping")
        return None
    return doc


def validate_repo(root: Path = REPO_ROOT) -> list[str]:
    """Return every static invariant violation in the repository (empty means valid)."""
    errors: list[str] = []
    graph_raw = _load(root, "pipeline/graph.yaml", errors)
    budgets = _load(root, "policy/budgets.yaml", errors)
    routing = _load(root, "policy/routing.yaml", errors)
    policy_docs = {name: _load(root, f"policy/{name}.yaml", errors) for name in POLICY_FILES}
    registry = _load(root, "policy/model_registry.yaml", errors)

    graph = Graph.from_mapping(graph_raw) if graph_raw is not None else None
    if graph is not None and budgets is not None and routing is not None:
        errors += validate(graph, budgets, routing)
    if all(doc is not None for doc in policy_docs.values()):
        errors += validate_policies(policy_docs)
    if graph is not None and registry is not None:
        errors += validate_registry(registry, graph, root)
    if policy_docs["data_classes"] is not None and registry is not None:
        errors += check_artifact_defaults(policy_docs["data_classes"], registry)
    return errors
