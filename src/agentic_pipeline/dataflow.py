"""Judge a proposal's declared data flows against policy/data_classes.yaml.

A flow is `{id, data_class, destination}`. `destinations` in the policy says which
destination kinds are cloud-side. A flow to a cloud-side destination is a violation when its
effective data class is not `cloud_allowed`. The effective class is the more restrictive of
the flow's declared class and the entry spec's `declared_classification`, so a proposal
cannot label data below what grounding recorded. Anything undecidable (unknown class or
destination, or classification still unresolved in the entry spec) is `None`, never clear.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def _rank(order: list[str], name: str) -> int:
    return order.index(name)


def flow_verdict(
    flow: Mapping[str, Any], policy: Mapping[str, Any], entry_data: Mapping[str, Any]
) -> tuple[bool | None, str]:
    """(True violation | False clear | None cannot tell, reason)."""
    label = str(flow.get("id", "?"))
    classes = policy["classes"]
    order = policy["order"]
    destinations = policy.get("destinations") or {}
    dest = destinations.get(flow.get("destination"))
    if dest is None:
        return None, f"{label}: destination {flow.get('destination')!r} is not defined in policy"
    if flow.get("data_class") not in classes:
        return None, f"{label}: data class {flow.get('data_class')!r} is not defined in policy"
    if not dest.get("cloud"):
        return False, f"{label}: stays on the host or inside the organization"
    effective = flow["data_class"]
    declared = entry_data.get("declared_classification")
    if declared is not None:
        if declared not in classes:
            return None, f"{label}: entry spec classification {declared!r} is not defined"
        if _rank(order, declared) > _rank(order, effective):
            effective = declared
    if not classes[effective].get("cloud_allowed"):
        return True, f"{label}: {effective} data may not go to {flow['destination']}"
    if entry_data.get("unresolved_classification"):
        return None, f"{label}: the entry spec still has unresolved classifications"
    return False, f"{label}: {effective} data may go to {flow['destination']}"


def candidate_verdict(
    doc: Mapping[str, Any], policy: Mapping[str, Any], entry_data: Mapping[str, Any]
) -> bool | None:
    """A violation anywhere fires; any undecided flow is None; absent `data_flows` is None.

    An explicit empty list says the design moves no data to any destination and is clear.
    """
    flows = doc.get("data_flows")
    if flows is None:
        return None
    verdicts = [flow_verdict(f, policy, entry_data)[0] for f in flows]
    if any(v is True for v in verdicts):
        return True
    return None if any(v is None for v in verdicts) else False
