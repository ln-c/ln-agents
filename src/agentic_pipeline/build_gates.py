"""Deterministic build-side gates: E2 (checks), X (exception router), E3 (escalation).

E2 reads the newest implementation patch (the `implementation_patch` artifact in the E1 or
E4 stage manifest, re-hashed against it) and runs the checks that need only files. Every
check ends `passed`, `failed`, or `not_evaluated`; E2 passes only if all of them passed, so
a check that cannot run (tests, contracts, perf need the E0 sandbox, which does not exist
yet) fails closed and the run goes to X.

X reproduces E2's result (the checks are a pure function of the patch and sealed inputs),
classifies it, and picks a cause from `policy/routing.yaml`:
* a security-relevant failure (write scope, protected tests or contracts changed, a secret,
  a dependency change) routes to `security_or_scope`, i.e. H1;
* a failed behavioral check routes to `implementation_violates_contract` (E1, `local` budget);
* anything else, including a check that could not run, routes to `unknown_or_spent` (H1):
  retrying cannot change a result that was never computed, so it must not spend a budget.
X never returns `local_spent` (the runner takes that route when the budget is spent) and
cannot reach the causes that need judgement (`contract_inadequate`, `requirement_missing`,
`topology_conflict_*`) or an infrastructure fault (`infra_rerun_*`); those stay unreachable
until something can establish them. X coming from F1 blocks until F1 has a handler.

E3 sends a run to E4 only if E4 is enabled in the registry and some agent in
`policy/escalation.yaml` may see the run's most restrictive data class. E4 is disabled
today, so E3 always returns `escalation_not_allowed`, with the reasons recorded.

The "budget remains" half of E3 is the runner's: the escalation edge carries the budget.
Audit records go under `gates/<NODE>/<input digest>.json`.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any

from agentic_pipeline.gates import (
    UNEVALUATED,
    GateError,
    _digest,
    _policy,
    _record,
)
from agentic_pipeline.grounding import G1_SOURCES
from agentic_pipeline.manifest import (
    ManifestError,
    digest_path,
    load_manifest,
    resolve_sealed_inputs,
)

if TYPE_CHECKING:
    from agentic_pipeline.runner import HandlerContext

PATCH_STAGES = ("E1", "E4")
PATCH_ARTIFACT = "implementation_patch"
MAX_SCAN_BYTES = 1 << 20

# Repo subtrees E1 may never write (graph X.forbidden_mutations, plus the human-owned
# trees and CODEOWNERS). The tests and contracts subtrees come from the G1 artifact map.
FORBIDDEN_TREES = (
    G1_SOURCES["protected_tests"],
    G1_SOURCES["applicable_contracts"],
    G1_SOURCES["adr_snapshot"],
    "policy",
    "schemas",
    "pipeline",
    ".github",
)
DEPENDENCY_FILES = re.compile(
    r"(^|/)(pyproject\.toml|uv\.lock|requirements[^/]*\.txt|setup\.(py|cfg)|Pipfile(\.lock)?"
    r"|poetry\.lock|package(-lock)?\.json|yarn\.lock|pnpm-lock\.yaml|go\.(mod|sum)"
    r"|Cargo\.(toml|lock)|Gemfile(\.lock)?)$"
)
SECRET_PATTERNS = {
    "private_key": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    "aws_access_key": re.compile(r"\b(AKIA|ASIA)[0-9A-Z]{16}\b"),
    "github_token": re.compile(r"\bgh[pousr]_[A-Za-z0-9]{36,}\b"),
    "slack_token": re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}\b"),
    "api_key_assignment": re.compile(
        r"(?i)\b(api[_-]?key|secret|token|passw(or)?d)\b\s*[:=]\s*['\"][^'\"\s]{16,}['\"]"
    ),
}

PASSED, FAILED = "passed", "failed"
# Behavioral checks need a sandbox that runs the code; until E0 exists they cannot run.
BEHAVIORAL = {
    "contract_tests": "needs the E0 sandbox to execute",
    "acceptance_tests": "needs the E0 sandbox to execute",
    "performance": "needs the E0 sandbox to execute",
}
# Failed check -> routing cause; the first matching cause wins.
CAUSES: tuple[tuple[str, tuple[str, ...]], ...] = (
    (
        "security_or_scope",
        (
            "write_scope",
            "protected_tests_intact",
            "contracts_intact",
            "no_secrets",
            "no_dependency_change",
        ),
    ),
    ("implementation_violates_contract", tuple(BEHAVIORAL)),
)
UNKNOWN_CAUSE = "unknown_or_spent"


class BuildGateError(GateError):
    pass


# ── E2 ───────────────────────────────────────────────────────────────────────


def _latest_patch(ctx: HandlerContext) -> tuple[str, Path, str]:
    """(stage, directory, digest) of the newest patch, re-hashed against its manifest."""
    found: list[tuple[str, str, Mapping[str, Any]]] = []
    for stage in PATCH_STAGES:
        try:
            doc = load_manifest(ctx.run_root, stage, ctx.run_id)
        except ManifestError as exc:
            raise BuildGateError(str(exc)) from exc
        if doc is not None and PATCH_ARTIFACT in doc["artifacts"]:
            found.append((doc["published_at"], stage, doc["artifacts"][PATCH_ARTIFACT]))
    if not found:
        raise BuildGateError("no implementation patch has been published by E1 or E4")
    _, stage, entry = max(found, key=lambda item: item[0])
    path = (ctx.run_root / entry["path"]).resolve()
    if not path.is_relative_to(ctx.run_root.resolve()):
        raise BuildGateError(f"{stage}: patch path escapes the run root")
    try:
        actual = digest_path(path)
    except ManifestError as exc:
        raise BuildGateError(f"{stage}: {exc}") from exc
    if actual != entry["digest"]:
        raise BuildGateError(f"{stage}: patch changed since the stage published it")
    return stage, path, actual


def _files(patch: Path) -> list[Path]:
    return sorted(p for p in patch.rglob("*") if p.is_file())


def _write_scope(patch: Path) -> list[str]:
    problems = []
    for file in _files(patch):
        rel = PurePosixPath(file.relative_to(patch).as_posix())
        if rel.is_absolute() or ".." in rel.parts:
            problems.append(f"{rel}: path leaves the repository")
        elif rel.name == "CODEOWNERS" or any(
            rel == PurePosixPath(tree) or PurePosixPath(tree) in rel.parents
            for tree in FORBIDDEN_TREES
        ):
            problems.append(f"{rel}: path is human-owned or protected")
    return problems


def _secrets(patch: Path) -> list[str]:
    problems = []
    for file in _files(patch):
        rel = file.relative_to(patch).as_posix()
        text = file.read_bytes()[:MAX_SCAN_BYTES].decode("utf-8", errors="replace")
        problems += [f"{rel}: {kind}" for kind, rx in SECRET_PATTERNS.items() if rx.search(text)]
    return problems


def _dependencies(patch: Path) -> list[str]:
    names = (f.relative_to(patch).as_posix() for f in _files(patch))
    return [
        f"{n}: dependency manifest or lockfile changed" for n in names if DEPENDENCY_FILES.search(n)
    ]


def _sealed_intact(ctx: HandlerContext, artifact: str) -> list[str]:
    """Problems with a G1 artifact: a tampered one is a failure, an absent one blocks."""
    _, errors = resolve_sealed_inputs(ctx.run_root, ctx.run_id, ctx.registry, [artifact])
    if any("content changed" in e for e in errors):
        return errors
    if errors:
        raise BuildGateError("; ".join(errors))
    return []


def run_checks(ctx: HandlerContext) -> tuple[str, dict[str, dict[str, Any]]]:
    """(patch digest, check name -> {status, problems}); a pure function of the inputs."""
    _policy(ctx)
    stage, patch, digest = _latest_patch(ctx)
    scans: dict[str, list[str]] = {
        "write_scope": _write_scope(patch),
        "protected_tests_intact": _sealed_intact(ctx, "protected_tests"),
        "contracts_intact": _sealed_intact(ctx, "applicable_contracts"),
        "no_secrets": _secrets(patch),
        "no_dependency_change": _dependencies(patch),
    }
    checks: dict[str, dict[str, Any]] = {
        name: {"status": FAILED if problems else PASSED, "problems": problems}
        for name, problems in scans.items()
    }
    for name, reason in BEHAVIORAL.items():
        checks[name] = {"status": UNEVALUATED, "problems": [reason]}
    return f"{stage}:{digest}", checks


def check_e2(ctx: HandlerContext) -> str:
    """E2: every check must have passed; anything else goes to the exception router."""
    patch, checks = run_checks(ctx)
    guard = (
        "all_pass"
        if all(c["status"] == PASSED for c in checks.values())
        else "any_fail_or_exception"
    )
    _record(
        ctx,
        "E2",
        _digest({"patch": patch, "checks": checks}),
        {
            "patch": patch,
            "guard": guard,
            "checks": checks,
        },
    )
    return guard


# ── X ────────────────────────────────────────────────────────────────────────


def classify(checks: Mapping[str, Mapping[str, Any]]) -> str:
    """Cause for a set of check results; unknown unless a failed check names one."""
    failed = {name for name, c in checks.items() if c["status"] == FAILED}
    for cause, names in CAUSES:
        if failed & set(names):
            return cause
    return UNKNOWN_CAUSE


def route_exception(ctx: HandlerContext) -> str:
    """X: reproduce the E2 result, classify it, and return a cause that policy defines."""
    origin = ctx.entered_from()
    if origin != "E2":
        raise BuildGateError(f"X entered from {origin!r}; only E2 failures can be routed yet")
    policy = _policy(ctx)
    patch, checks = run_checks(ctx)
    cause = classify(checks)
    if cause not in policy.routing["routes"]:
        raise BuildGateError(f"cause {cause!r} is not defined in policy/routing.yaml")
    _record(
        ctx,
        "X",
        _digest({"patch": patch, "checks": checks}),
        {
            "patch": patch,
            "from": origin,
            "cause": cause,
            "checks": {n: c["status"] for n, c in checks.items()},
        },
    )
    return cause


# ── E3 ───────────────────────────────────────────────────────────────────────

E3_INPUTS = (
    "pinned_context_manifest",
    "pinned_source_tree",
    "applicable_contracts",
    "protected_tests",
    "contracts_sealed",
    "generated_tests_sealed",
)


def escalation_facts(ctx: HandlerContext) -> dict[str, Any]:
    policy = _policy(ctx)
    inputs, errors = resolve_sealed_inputs(ctx.run_root, ctx.run_id, ctx.registry, list(E3_INPUTS))
    order = policy.data_classes["order"]
    classes = [i.data_class for i in inputs.values()] + ["unknown"] * bool(errors)
    data_class = max(
        classes or ["unknown"], key=lambda c: order.index(c) if c in order else len(order)
    )
    declared = policy.data_classes["classes"]
    eligible = [
        a["id"]
        for a in policy.escalation["agents"]
        if data_class in a["allowed_data_classes"]
        and (a["location"] == "local" or declared.get(data_class, {}).get("cloud_allowed") is True)
    ]
    e4 = policy.registry["stage_requirements"]["E4"]
    return {
        "data_class": data_class,
        "unresolved_inputs": errors,
        "eligible_agents": eligible,
        "e4_enabled": e4.get("mode") != "disabled",
        "e4_reason": e4.get("reason"),
    }


def decide_escalation(ctx: HandlerContext) -> str:
    facts = escalation_facts(ctx)
    allowed = bool(facts["eligible_agents"]) and facts["e4_enabled"]
    guard = "escalation_allowed" if allowed else "escalation_not_allowed"
    _record(ctx, "E3", _digest(facts), {"guard": guard, **facts})
    return guard


BUILD_GATE_HANDLERS: dict[str, Callable[[Any], str]] = {
    "E2": check_e2,
    "X": route_exception,
    "E3": decide_escalation,
}
