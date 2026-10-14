"""The pinned-snapshot matcher behind the known_vulnerable_dependency veto."""

from __future__ import annotations

import copy
from typing import Any

import pytest

from agentic_pipeline.advisories import candidate_verdict, dependency_verdict
from agentic_pipeline.policies import check_advisories

SNAPSHOT: dict[str, Any] = {
    "version": 1,
    "source": "osv",
    "generated_at": "2026-10-14",
    "ecosystems": ["pypi"],
    "advisories": [
        {
            "id": "GHSA-1",
            "severity": "critical",
            "affected": [
                {
                    "package": {"ecosystem": "PyPI", "name": "Bad_Lib"},
                    "ranges": [
                        {
                            "type": "ECOSYSTEM",
                            "events": [
                                {"introduced": "1.0"},
                                {"fixed": "1.4"},
                                {"introduced": "2.0"},
                            ],
                        }
                    ],
                }
            ],
        },
        {
            "id": "GHSA-2",
            "severity": "critical",
            "affected": [
                {"package": {"ecosystem": "PyPI", "name": "listed"}, "versions": ["3.1.0"]}
            ],
        },
        {
            "id": "GHSA-3",
            "severity": "high",
            "affected": [
                {
                    "package": {"ecosystem": "PyPI", "name": "minor"},
                    "ranges": [{"type": "ECOSYSTEM", "events": [{"introduced": "0"}]}],
                }
            ],
        },
    ],
}


def dep(name: str, version: str | None = "1.0", ecosystem: str = "pypi") -> dict[str, Any]:
    out: dict[str, Any] = {"name": name, "ecosystem": ecosystem}
    if version is not None:
        out["version"] = version
    return out


@pytest.mark.parametrize(
    ("version", "expected"),
    [("0.9", False), ("1.0", True), ("1.3.9", True), ("1.4", False), ("1.9", False), ("2.5", True)],
)
def test_range_events_follow_osv_semantics(version: str, expected: bool) -> None:
    assert dependency_verdict(dep("bad-lib", version), SNAPSHOT)[0] is expected


def test_names_are_normalised_and_explicit_versions_match() -> None:
    assert dependency_verdict(dep("BAD.LIB", "1.1"), SNAPSHOT)[0] is True
    assert dependency_verdict(dep("listed", "3.1.0"), SNAPSHOT)[0] is True
    assert dependency_verdict(dep("listed", "3.1.1"), SNAPSHOT)[0] is False


def test_only_critical_advisories_veto() -> None:
    assert dependency_verdict(dep("minor", "1.0"), SNAPSHOT)[0] is False


@pytest.mark.parametrize(
    "dependency",
    [dep("x", ecosystem="npm"), dep("bad-lib", None), dep("bad-lib", "not a version")],
)
def test_what_the_snapshot_cannot_decide_is_none_never_clear(dependency: dict[str, Any]) -> None:
    assert dependency_verdict(dependency, SNAPSHOT)[0] is None


def test_an_uncovered_ecosystem_is_not_decided_even_with_advisories() -> None:
    empty = {**SNAPSHOT, "ecosystems": []}
    assert dependency_verdict(dep("bad-lib", "1.1"), empty)[0] is None


def test_an_unsupported_range_type_is_not_decided() -> None:
    odd = copy.deepcopy(SNAPSHOT)
    odd["advisories"][0]["affected"][0]["ranges"][0]["type"] = "GIT"
    assert dependency_verdict(dep("bad-lib", "9.9"), odd)[0] is None


def test_candidate_verdicts() -> None:
    assert candidate_verdict({}, SNAPSHOT) is None  # did not say: undetermined
    assert candidate_verdict({"dependencies": []}, SNAPSHOT) is False
    assert candidate_verdict({"dependencies": [dep("ok", "1")]}, SNAPSHOT) is False
    assert candidate_verdict({"dependencies": [dep("bad-lib", "1.1")]}, SNAPSHOT) is True
    mixed = {"dependencies": [dep("ok", "1"), dep("x", ecosystem="npm")]}
    assert candidate_verdict(mixed, SNAPSHOT) is None
    both = {"dependencies": [dep("x", ecosystem="npm"), dep("bad-lib", "1.1")]}
    assert candidate_verdict(both, SNAPSHOT) is True  # a hit outranks an unknown


def test_the_snapshot_is_validated() -> None:
    assert check_advisories(SNAPSHOT) == []
    broken = copy.deepcopy(SNAPSHOT)
    broken["generated_at"] = None
    broken["ecosystems"] = ["npm"]
    broken["advisories"][1]["severity"] = "bad"
    broken["advisories"][2]["id"] = "GHSA-1"
    errors = "\n".join(check_advisories(broken))
    for fragment in ("not supported", "generated_at", "severity", "duplicate id"):
        assert fragment in errors
