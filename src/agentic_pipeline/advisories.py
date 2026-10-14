"""Match declared dependencies against the pinned advisory snapshot (policy/advisories.yaml).

Pure functions, no network. A dependency is *affected* only if the snapshot covers its
ecosystem, its version parses, and a critical advisory's ranges or version list include it.
Anything the snapshot cannot decide (uncovered ecosystem, missing or unparseable version)
is `None`: not evaluated, never treated as clear.

Only PyPI versions are understood (PEP 440 via `packaging`); the snapshot lists PyPI in
`ecosystems` only if it has been imported for it.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version

SUPPORTED = ("pypi",)
SEVERITIES = ("critical", "high", "moderate", "low")


def _parse(text: Any) -> Version | None:
    try:
        return Version(str(text))
    except InvalidVersion:
        return None


def _in_range(version: Version, events: list[Mapping[str, Any]]) -> bool | None:
    """OSV ECOSYSTEM range semantics; None if an event version does not parse."""
    parsed: list[tuple[Version, str]] = []
    for event in events:
        (kind, raw), *_ = event.items()
        value = Version("0") if (kind == "introduced" and str(raw) == "0") else _parse(raw)
        if value is None:
            return None
        parsed.append((value, kind))
    affected = False
    for value, kind in sorted(parsed, key=lambda item: item[0]):
        if kind == "introduced" and version >= value:
            affected = True
        elif kind == "fixed" and version >= value:
            affected = False
        elif kind == "last_affected" and version > value:
            affected = False
    return affected


def _hits(entry: Mapping[str, Any], name: str, version: Version) -> bool | None:
    package = entry.get("package") or {}
    if str(package.get("ecosystem", "")).lower() != "pypi":
        return False
    if canonicalize_name(str(package.get("name", ""))) != name:
        return False
    if any(_parse(v) == version for v in entry.get("versions") or []):
        return True
    for rng in entry.get("ranges") or []:
        if rng.get("type") != "ECOSYSTEM":
            return None  # a range type this module cannot evaluate
        verdict = _in_range(version, rng.get("events") or [])
        if verdict is None or verdict:
            return verdict
    return False


def dependency_verdict(
    dependency: Mapping[str, Any], snapshot: Mapping[str, Any]
) -> tuple[bool | None, str]:
    """(True affected | False clear | None cannot tell, reason)."""
    ecosystem = str(dependency.get("ecosystem", "")).lower()
    name = str(dependency.get("name", ""))
    covered = {str(e).lower() for e in snapshot.get("ecosystems") or []}
    if ecosystem not in SUPPORTED or ecosystem not in covered:
        return None, f"{name}: the advisory snapshot does not cover ecosystem {ecosystem!r}"
    if dependency.get("version") is None:
        return None, f"{name}: no version declared"
    version = _parse(dependency["version"])
    if version is None:
        return None, f"{name}: version {dependency['version']!r} is not a PEP 440 version"
    canonical = canonicalize_name(name)
    undecided = False
    for advisory in snapshot.get("advisories") or []:
        if advisory.get("severity") != "critical":
            continue
        for entry in advisory.get("affected") or []:
            verdict = _hits(entry, canonical, version)
            if verdict:
                return True, f"{name} {dependency['version']}: affected by {advisory['id']}"
            undecided = undecided or verdict is None
    if undecided:
        return None, f"{name}: an advisory range could not be evaluated"
    return False, f"{name} {dependency['version']}: no critical advisory"


def candidate_verdict(doc: Mapping[str, Any], snapshot: Mapping[str, Any]) -> bool | None:
    """Veto verdict for one proposal: any affected dependency fires; any undecided one is None.

    A proposal that does not say whether it adds dependencies (`dependencies` absent) is
    undecided; an explicit empty list is clear.
    """
    declared = doc.get("dependencies")
    if declared is None:
        return None
    verdicts = [dependency_verdict(d, snapshot)[0] for d in declared]
    if any(v is True for v in verdicts):
        return True
    return None if any(v is None for v in verdicts) else False
