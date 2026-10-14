"""Judge a proposal's declared contract changes against the contracts pinned at grounding.

A proposal lists `contracts_touched`: `{path, change}` with change `none`, `compatible` or
`breaking`. The pinned context manifest's `contract_versions` says which contract files exist.
A `breaking` change to a pinned contract fires the veto. No approved amendment can exist yet
(signed approvals are not built), so there is no way to clear one: it always fires. A path
that is not pinned cannot be judged and is `None`, never clear, as is an absent list. An
explicit empty list says the design touches no contract.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def touch_verdict(touch: Mapping[str, Any], pinned: Mapping[str, str]) -> tuple[bool | None, str]:
    """(True breaks a pinned contract | False clear | None cannot tell, reason)."""
    path = str(touch.get("path", "?"))
    if path not in pinned:
        return None, f"{path}: not a contract pinned at the grounded SHA"
    change = touch.get("change")
    if change == "breaking":
        return True, f"{path}: breaking change with no approved amendment"
    if change in ("none", "compatible"):
        return False, f"{path}: {change}"
    return None, f"{path}: change {change!r} is not recognised"


def candidate_verdict(doc: Mapping[str, Any], pinned: Mapping[str, str]) -> bool | None:
    touched = doc.get("contracts_touched")
    if touched is None:
        return None
    verdicts = [touch_verdict(t, pinned)[0] for t in touched]
    if any(v is True for v in verdicts):
        return True
    return None if any(v is None for v in verdicts) else False
