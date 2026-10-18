"""Deterministic build-side gates: E2 and F1 (checks), X (exception router), E3 (escalation).

E2 reads the newest implementation patch (the `implementation_patch` artifact in the E1 or
E4 stage manifest, re-hashed against it) and runs the static checks that need only files,
then the E2 checks of `policy/ci.yaml` in the offline sandbox (see ci.py). No behavioral
check runs unless every static check passed. Every check ends `passed`, `failed`,
`not_evaluated`, `skipped` (optional and unset) or `infra_error`; a stage passes only if every
check passed or was skipped, so a required check that is unset fails closed. F1 is the same
gate with the F1 checks (independent CI: integration, mutation testing). The E2
checks include dead code and MC/DC over the patch: code the protected tests cannot exercise is
the implementation's fault, so it goes back to E1.

X reproduces the origin's result (E2 or F1; the checks are a function of the patch, sealed
inputs and frozen policy), classifies it, and picks a cause from `policy/routing.yaml`:
* a security-relevant failure (write scope, protected tests or contracts changed, a secret,
  a dependency change) routes to `security_or_scope`, i.e. H1;
* a failed behavioral check routes to the `cause` policy/ci.yaml gives it;
* a sandbox or toolchain problem routes to `infra_rerun_e2` / `infra_rerun_f1`;
* anything else, including a required check that never ran, routes to `unknown_or_spent`
  (H1): retrying cannot change a result that was never computed, so it spends no budget.
X never returns `local_spent` (the runner takes that route when the budget is spent) and
cannot reach the causes that need judgement (`contract_inadequate`, `topology_conflict_*`).

E3 sends a run to E4 only if E4 is enabled in the registry and some agent in
`policy/escalation.yaml` may see the run's most restrictive data class. E4 is disabled
today, so E3 always returns `escalation_not_allowed`, with the reasons recorded.

The "budget remains" half of E3 is the runner's: the escalation edge carries the budget.
Audit records go under `gates/<NODE>/<input digest>.json`.
"""

from __future__ import annotations

import json
import re
import shutil
import tempfile
from collections.abc import Callable, Mapping
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any

from agentic_pipeline.ci import (
    FAILED as CI_FAILED,
)
from agentic_pipeline.ci import (
    INFRA,
    OK_STATUSES,
    run_stage,
    stage_checks,
)
from agentic_pipeline.gates import (
    UNEVALUATED,
    GateError,
    _digest,
    _policy,
    _record,
    _sealed,
)
from agentic_pipeline.grounding import PINNED_SOURCES, GroundingError, _extract
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
# trees and CODEOWNERS). The tests and contracts subtrees come from the pinned artifact map.
FORBIDDEN_TREES = (
    PINNED_SOURCES["protected_tests"],
    PINNED_SOURCES["applicable_contracts"],
    "policy",
    "schemas",
    "pipeline",
    "scripts",
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
CI_WORK = "ci_work"
# Failed static check -> routing cause. Failed behavioral checks name their own cause in
# policy/ci.yaml; an infra problem is re-run; anything else goes to a human.
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
    """Problems with a grounding artifact: a tampered one is a failure, an absent one blocks."""
    _, errors = resolve_sealed_inputs(ctx.run_root, ctx.run_id, ctx.registry, [artifact])
    if any("content changed" in e for e in errors):
        return errors
    if errors:
        raise BuildGateError("; ".join(errors))
    return []


REBASED = "constraints_unchanged_rebase"


def latest_rebase(run_root: Path) -> dict[str, Any] | None:
    """The newest RC record that rebased the candidate onto a newer target tip, if any."""
    records = sorted((run_root / "gates" / "RC").glob("*.json"), key=lambda p: p.stat().st_mtime_ns)
    for path in reversed(records):
        doc = json.loads(path.read_text("utf-8"))
        if doc.get("guard") == REBASED:
            return doc
    return None


def tested_base(ctx: HandlerContext) -> str:
    """The commit the candidate is tested against: the pinned one, or the tip RC rebased onto."""
    rebase = latest_rebase(ctx.run_root)
    if rebase is not None:
        return str(rebase["new_base"])
    manifest = _sealed(ctx, ["pinned_context_manifest"])["pinned_context_manifest"]
    return str(json.loads((manifest / "context_manifest.json").read_text("utf-8"))["commit_sha"])


def _run_behavioral(
    ctx: HandlerContext, stage: str, patch: Path, static_ok: bool
) -> dict[str, dict[str, Any]]:
    """Policy checks for `stage`; none run unless every static check passed."""
    ci = _policy(ctx).ci
    specs = stage_checks(ci, stage)
    if not static_ok:
        return {
            name: {
                "status": UNEVALUATED,
                "problems": ["not run: a static check failed"],
                "cause": spec["cause"],
            }
            for name, spec in specs.items()
        }
    scratch = ctx.run_root / CI_WORK
    scratch.mkdir(exist_ok=True)
    work = Path(tempfile.mkdtemp(prefix=f"{stage}-", dir=scratch))
    try:
        if latest_rebase(ctx.run_root) is None:
            source = _sealed(ctx, ["pinned_source_tree"])["pinned_source_tree"]
        else:  # RC moved the base: test the patch on the new tip, read from git objects
            source = work / "base"
            try:
                _extract(ctx.root, tested_base(ctx), None, source)
            except GroundingError as exc:
                raise BuildGateError(f"cannot read the rebased tree: {exc}") from exc
        return run_stage(ci, stage, source, patch, work)
    finally:
        shutil.rmtree(work, ignore_errors=True)


def run_checks(ctx: HandlerContext, stage: str = "E2") -> tuple[str, dict[str, dict[str, Any]]]:
    """(patch digest, check name -> {status, problems}) for `stage`'s checks."""
    _policy(ctx)
    origin, patch, digest = _latest_patch(ctx)
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
    static_ok = all(c["status"] == PASSED for c in checks.values())
    checks.update(_run_behavioral(ctx, stage, patch, static_ok))
    return f"{origin}:{digest}", checks


def _stage_gate(ctx: HandlerContext, stage: str, passed: str, failed: str) -> str:
    patch, checks = run_checks(ctx, stage)
    guard = passed if all(c["status"] in OK_STATUSES for c in checks.values()) else failed
    _record(
        ctx,
        stage,
        _digest({"patch": patch, "checks": {n: c["status"] for n, c in checks.items()}}),
        {
            "patch": patch,
            "base": tested_base(ctx),
            "guard": guard,
            "checks": checks,
        },
    )
    return guard


def check_e2(ctx: HandlerContext) -> str:
    """E2: every check must have passed or been optional and unset; else the router decides."""
    return _stage_gate(ctx, "E2", "all_pass", "any_fail_or_exception")


def check_f1(ctx: HandlerContext) -> str:
    """F1: independent CI on the same patch; reached from E2, or from X for an infra re-run."""
    origin = ctx.entered_from()
    if origin not in ("E2", "X"):
        raise BuildGateError(f"F1 entered from {origin!r}; only E2 or X lead here")
    return _stage_gate(ctx, "F1", "all_required_pass", "required_check_failed")


# ── X ────────────────────────────────────────────────────────────────────────


def classify(checks: Mapping[str, Mapping[str, Any]], origin: str = "E2") -> str:
    """Cause for a set of check results: security first, then a failed check's own cause,
    then an infra re-run; unknown otherwise (a required check that never ran is unknown)."""
    failed = {name for name, c in checks.items() if c["status"] == FAILED}
    for cause, names in CAUSES:
        if failed & set(names):
            return cause
    for c in checks.values():
        if c["status"] == CI_FAILED and "cause" in c:
            return str(c["cause"])
    if any(c["status"] == INFRA for c in checks.values()):
        return f"infra_rerun_{origin.lower()}"
    return UNKNOWN_CAUSE


def route_exception(ctx: HandlerContext) -> str:
    """X: reproduce the E2 result, classify it, and return a cause that policy defines."""
    origin = ctx.entered_from()
    if origin not in ("E2", "F1"):
        raise BuildGateError(f"X entered from {origin!r}; only E2 and F1 failures can be routed")
    policy = _policy(ctx)
    patch, checks = run_checks(ctx, origin)
    cause = classify(checks, origin)
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
    "F1": check_f1,
    "X": route_exception,
    "E3": decide_escalation,
}
