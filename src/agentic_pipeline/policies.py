"""Invariants for human-owned policy files not covered by `graph.validate`.

Budgets and routing are checked against the graph in `graph.validate`; this
module covers weights, vetoes, data classes, escalation, task classes, and the fast path.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from typing import Any

MUST_BLOCK_CLOUD = ("unknown", "confidential", "restricted")
POLICY_FILES = ("weights", "vetoes", "data_classes", "escalation", "fast_path", "task_classes")


def check_weights(doc: Mapping[str, Any]) -> list[str]:
    errors: list[str] = []
    weights = doc.get("weights") or {}
    if not weights:
        errors.append("weights: no weights defined")
    for name, value in weights.items():
        if not isinstance(value, int | float) or value <= 0:
            errors.append(f"weights: {name} must be a positive number, got {value!r}")
    numeric = [v for v in weights.values() if isinstance(v, int | float)]
    if numeric and not math.isclose(sum(numeric), 1.0, rel_tol=1e-9, abs_tol=1e-9):
        errors.append(f"weights: must sum to 1.0, got {sum(numeric)}")
    thresholds = doc.get("thresholds") or {}
    for key in ("near_tie_delta", "high_uncertainty"):
        value = thresholds.get(key)
        if not isinstance(value, int | float) or not 0 < value < 1:
            errors.append(f"weights: thresholds.{key} must be in (0, 1), got {value!r}")
    return errors


def check_vetoes(doc: Mapping[str, Any]) -> list[str]:
    errors: list[str] = []
    vetoes = doc.get("vetoes") or []
    if not vetoes:
        errors.append("vetoes: no vetoes defined")
    seen: set[str] = set()
    for i, veto in enumerate(vetoes):
        veto_id = veto.get("id")
        if not veto_id:
            errors.append(f"vetoes[{i}]: missing id")
        elif veto_id in seen:
            errors.append(f"vetoes: duplicate id {veto_id!r}")
        seen.add(veto_id)
        if not veto.get("description"):
            errors.append(f"vetoes[{i}]: missing description")
    return errors


def check_data_classes(doc: Mapping[str, Any]) -> list[str]:
    errors: list[str] = []
    classes = doc.get("classes") or {}
    order = doc.get("order") or []
    if len(order) != len(set(order)) or set(order) != set(classes):
        errors.append(f"data_classes: order {order} must list each class exactly once")
    for name in MUST_BLOCK_CLOUD:
        if (classes.get(name) or {}).get("cloud_allowed") is not False:
            errors.append(f"data_classes: {name} must have cloud_allowed: false")
    return errors


def check_escalation(doc: Mapping[str, Any], data_classes: Mapping[str, Any]) -> list[str]:
    errors: list[str] = []
    classes = data_classes.get("classes") or {}
    seen: set[str] = set()
    for i, agent in enumerate(doc.get("agents") or []):
        agent_id = agent.get("id") or f"agents[{i}]"
        if agent_id in seen:
            errors.append(f"escalation: duplicate agent id {agent_id!r}")
        seen.add(agent_id)
        for cls in agent.get("allowed_data_classes") or []:
            if cls not in classes:
                errors.append(f"escalation {agent_id}: unknown data class {cls!r}")
            elif agent.get("location") == "cloud" and not classes[cls].get("cloud_allowed"):
                errors.append(f"escalation {agent_id}: cloud agent allows non-cloud class {cls!r}")
    return errors


def check_fast_path(doc: Mapping[str, Any]) -> list[str]:
    errors: list[str] = []
    if not isinstance(doc.get("task_class_allowlist"), list):
        errors.append("fast_path: task_class_allowlist must be a list")
    require_all = doc.get("require_all") or []
    if not require_all:
        errors.append("fast_path: require_all must not be empty")
    elif "no_active_veto" not in require_all:
        errors.append("fast_path: require_all must include no_active_veto")
    return errors


TASK_CLASS_ID = re.compile(r"[a-z][a-z0-9_]*")


def check_task_classes(doc: Mapping[str, Any], fast_path: Mapping[str, Any]) -> list[str]:
    errors: list[str] = []
    classes = doc.get("classes")
    if not isinstance(classes, dict) or not classes:
        return ["task_classes: classes must be a non-empty mapping"]
    for name, spec in classes.items():
        if not isinstance(name, str) or not TASK_CLASS_ID.fullmatch(name):
            errors.append(f"task_classes: {name!r} is not a lowercase snake_case id")
        if not isinstance(spec, dict) or not str(spec.get("description") or "").strip():
            errors.append(f"task_classes: {name} needs a non-empty description")
    allowlist = fast_path.get("task_class_allowlist")
    for name in allowlist if isinstance(allowlist, list) else []:
        if name not in classes:
            errors.append(f"fast_path: task_class_allowlist names unknown task class {name!r}")
    return errors


def validate_policies(docs: Mapping[str, Mapping[str, Any]]) -> list[str]:
    """Validate loaded policy documents keyed by file stem (see POLICY_FILES)."""
    return [
        *check_weights(docs["weights"]),
        *check_vetoes(docs["vetoes"]),
        *check_data_classes(docs["data_classes"]),
        *check_escalation(docs["escalation"], docs["data_classes"]),
        *check_fast_path(docs["fast_path"]),
        *check_task_classes(docs["task_classes"], docs["fast_path"]),
    ]
