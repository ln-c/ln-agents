"""Which blocking findings name a binding ADR."""

from __future__ import annotations

import pytest

from agentic_pipeline.adrs import binding_hits, named_adrs

ADRS = [
    {"id": "0001-pipeline-architecture", "status": "accepted"},
    {"id": "0002-open-decisions", "status": "proposed"},
    {"id": "0003-model-registry", "status": "implemented"},
    {"id": "0004-old", "status": "superseded"},
]


@pytest.mark.parametrize(
    ("violated", "expected"),
    [
        ("ADR 0001", ["0001-pipeline-architecture"]),
        ("adr-1 and ADR_0003", ["0001-pipeline-architecture", "0003-model-registry"]),
        ("0001-pipeline-architecture", ["0001-pipeline-architecture"]),
        ("ADR 00011", []),
        ("ADR 0010", []),
        ("a manifest constraint", []),
    ],
)
def test_named_adrs(violated: str, expected: list[str]) -> None:
    assert named_adrs(violated, ADRS) == expected


def finding(violated: str, severity: str = "blocking") -> dict:
    return {"id": "f", "severity": severity, "violated": violated}


def test_only_blocking_findings_naming_accepted_or_implemented_adrs_hit() -> None:
    critique = {
        "findings": [
            finding("ADR 0001"),
            finding("ADR 0002"),
            finding("ADR 0003"),
            finding("ADR 0004"),
            finding("ADR 0001", severity="non_blocking"),
        ]
    }
    assert binding_hits(critique, ADRS) == [
        "f: 0001-pipeline-architecture",
        "f: 0003-model-registry",
    ]
