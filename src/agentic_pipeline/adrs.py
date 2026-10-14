"""Find blocking review findings that name an accepted ADR as violated.

The veto never clears: whether a design contradicts an ADR is a semantic judgement, and a
model may not clear a hard veto. It fires only on evidence, a blocking finding from a valid
independent review whose `violated` field names an ADR the pinned manifest lists as accepted
or implemented. With no such finding the verdict stays undetermined and human review (H1)
decides. A finding that names a proposed, superseded or deprecated ADR, or none, does not fire.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from typing import Any

BINDING = frozenset({"accepted", "implemented"})
_NUMBERED = re.compile(r"(?<![0-9])adr[\s_-]*0*(\d+)(?![0-9])", re.IGNORECASE)


def _number(adr_id: str) -> int | None:
    head = adr_id.split("-", 1)[0]
    return int(head) if head.isdigit() else None


def named_adrs(violated: str, adrs: Sequence[Mapping[str, str]]) -> list[str]:
    """Ids of the manifest ADRs that `violated` refers to by full id or by `ADR 0001`."""
    cited = {int(n) for n in _NUMBERED.findall(violated)}
    return [
        a["id"]
        for a in adrs
        if a["id"] in violated or (_number(a["id"]) is not None and _number(a["id"]) in cited)
    ]


def binding_hits(critique: Mapping[str, Any], adrs: Sequence[Mapping[str, str]]) -> list[str]:
    """`finding id -> ADR id` strings for each blocking finding naming a binding ADR."""
    status = {a["id"]: a["status"] for a in adrs}
    hits = []
    for finding in critique.get("findings", []):
        if finding.get("severity") != "blocking":
            continue
        for adr in named_adrs(str(finding.get("violated", "")), adrs):
            if status[adr] in BINDING:
                hits.append(f"{finding.get('id', '?')}: {adr}")
    return hits
