"""Static repository validation: graph, policies, and model registry."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml

from agentic_pipeline.graph import REPO_ROOT, Graph, load_yaml, validate
from agentic_pipeline.policies import (
    POLICY_FILES,
    check_artifact_defaults,
    check_test_profiles,
    validate_policies,
)
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


def _capabilities(root: Path) -> list[str]:
    """The capability vocabulary, from the registry schema (the one place it is defined)."""
    try:
        schema = json.loads((root / "schemas" / "model_registry.schema.json").read_text("utf-8"))
        return list(schema["definitions"]["capability"]["enum"])
    except (OSError, ValueError, KeyError, TypeError):
        return []  # the registry check reports the unreadable schema


def validate_repo(root: Path = REPO_ROOT, policy_root: Path | None = None) -> list[str]:
    """Return every static invariant violation in the repository (empty means valid).

    The graph and policy are read from `policy_root` (a run's frozen copy); the schemas and the
    agent specs the registry names are checked in `root`.
    """
    errors: list[str] = []
    source = policy_root or root
    graph_raw = _load(source, "pipeline/graph.yaml", errors)
    budgets = _load(source, "policy/budgets.yaml", errors)
    routing = _load(source, "policy/routing.yaml", errors)
    policy_docs = {name: _load(source, f"policy/{name}.yaml", errors) for name in POLICY_FILES}
    registry = _load(source, "policy/model_registry.yaml", errors)

    graph = Graph.from_mapping(graph_raw) if graph_raw is not None else None
    if graph is not None and budgets is not None and routing is not None:
        errors += validate(graph, budgets, routing)
    if all(doc is not None for doc in policy_docs.values()):
        errors += validate_policies(policy_docs)
    if graph is not None and registry is not None:
        errors += validate_registry(registry, graph, root, policy_root)
    if policy_docs["model_test_profiles"] is not None:
        errors += check_test_profiles(policy_docs["model_test_profiles"], _capabilities(root))
    if policy_docs["data_classes"] is not None and registry is not None:
        errors += check_artifact_defaults(policy_docs["data_classes"], registry)
    return errors
