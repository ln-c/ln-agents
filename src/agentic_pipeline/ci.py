"""Run policy/ci.yaml checks against the pinned tree plus an implementation patch.

Each check runs offline in the bwrap sandbox on a fresh copy of the sealed pinned source tree
with the patch laid over it. The toolchain is a venv synced from that tree's `uv.lock`
outside the sandbox and bound read-only at /venv. This module executes nothing for a check
that is unset (see policy/ci.yaml): it is `not_evaluated` when required, `skipped` otherwise,
and never counts as passed. A sandbox or toolchain problem is `infra_error`, which X routes
to a re-run instead of blaming the implementation.
"""

from __future__ import annotations

import hashlib
import re
import shutil
import subprocess
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from agentic_pipeline.sandbox import SandboxSpec, SandboxUnavailable, run_sandboxed

PASSED, FAILED = "passed", "failed"
UNEVALUATED, SKIPPED, INFRA = "not_evaluated", "skipped", "infra_error"
OK_STATUSES = frozenset({PASSED, SKIPPED})
CHECK_NAMES = (
    "contract_tests",
    "acceptance_tests",
    "performance",
    "dead_code",
    "mcdc",
    "integration",
    "adr_fitness",
    "mutation",
)
STAGES = ("E2", "F1")
VENV = "/venv"
TAIL_CHARS = 2000
SYNC_TIMEOUT_SECONDS = 600


class CiInfraError(Exception):
    """The sandbox or toolchain could not be prepared; says nothing about the patch."""


def stage_checks(ci: Mapping[str, Any], stage: str) -> dict[str, Mapping[str, Any]]:
    return {n: spec for n, spec in ci["checks"].items() if spec["stage"] == stage}


def _score_unset(spec: Mapping[str, Any]) -> bool:
    score = spec.get("score")
    return score is not None and not (score.get("pattern") and score.get("minimum") is not None)


def is_unset(spec: Mapping[str, Any]) -> bool:
    return not spec.get("commands") or _score_unset(spec)


def _result(spec: Mapping[str, Any], status: str, problems: list[str], **extra: Any) -> dict:
    return {"status": status, "problems": problems, "cause": spec["cause"], **extra}


def prepare_tree(source: Path, patch: Path, dest: Path) -> None:
    """Copy the sealed source tree to `dest` and lay the patch files over it."""
    shutil.copytree(source, dest, symlinks=False)
    for file in sorted(p for p in patch.rglob("*") if p.is_file()):
        target = dest / file.relative_to(patch)
        if not target.resolve().is_relative_to(dest.resolve()):
            raise CiInfraError(f"{file.relative_to(patch)}: path leaves the tree")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(file, target)


def prepare_toolchain(tree: Path, venv: Path, toolchain: Mapping[str, Any]) -> None:
    """A venv from the tree's uv.lock, built outside the sandbox from the local cache."""
    if not (tree / "uv.lock").is_file():
        raise CiInfraError("the pinned tree has no uv.lock")
    uv = shutil.which("uv")
    if uv is None:
        raise CiInfraError("uv is not installed, so the pinned toolchain cannot be built")
    args = [uv, "sync", "--frozen", "--no-install-project", "--all-groups"]
    if toolchain.get("offline", True):
        args.append("--offline")
    try:
        done = subprocess.run(  # noqa: S603 - fixed uv argv, no shell
            args,
            cwd=tree,
            env={
                "PATH": "/usr/bin:/bin",
                "HOME": str(venv.parent),
                "UV_PROJECT_ENVIRONMENT": str(venv),
            },
            capture_output=True,
            text=True,
            timeout=SYNC_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise CiInfraError(f"toolchain sync failed: {exc}") from exc
    if done.returncode != 0:
        raise CiInfraError(f"toolchain sync failed: {done.stderr.strip()[-500:]}")


def _score(pattern: str, output: str) -> float | None:
    found = list(re.finditer(pattern, output))
    if not found or not found[-1].groups():
        return None
    try:
        return float(found[-1].group(1))
    except ValueError:
        return None


def run_check(
    name: str, spec: Mapping[str, Any], tree: Path, venv: Path | None, toolchain: Mapping[str, Any]
) -> dict[str, Any]:
    """Run one configured check; an unset one is not run."""
    if is_unset(spec):
        if spec["required"]:
            why = "no command" if not spec.get("commands") else "no score threshold"
            return _result(spec, UNEVALUATED, [f"{name}: required but unset ({why})"])
        return _result(spec, SKIPPED, [f"{name}: not configured and not required"])
    env = {
        "PATH": f"{VENV}/bin:/usr/bin" if venv else "/usr/bin",
        **toolchain.get("env", {}),
    }
    runs: list[dict[str, Any]] = []
    output = ""
    for argv in spec["commands"]:
        sandbox = SandboxSpec(
            argv=list(argv),
            write_path=tree,
            ro_binds={VENV: venv} if venv else {},
            env=env,
            timeout=float(spec.get("timeout_seconds", toolchain.get("timeout_seconds", 600))),
        )
        try:
            done = run_sandboxed(sandbox, None)
        except SandboxUnavailable as exc:
            return _result(spec, INFRA, [f"{name}: sandbox unavailable: {exc}"], runs=runs)
        output = done.stdout + done.stderr
        runs.append(
            {
                "argv": list(argv),
                "exit_code": done.returncode,
                "timed_out": done.timed_out,
                "output_sha256": hashlib.sha256(output.encode()).hexdigest(),
                "tail": output[-TAIL_CHARS:],
            }
        )
        if done.timed_out or done.returncode != 0:
            how = "timed out" if done.timed_out else f"exited {done.returncode}"
            return _result(spec, FAILED, [f"{name}: {' '.join(argv)} {how}"], runs=runs)
    if spec.get("score"):
        value = _score(spec["score"]["pattern"], output)
        minimum = spec["score"]["minimum"]
        if value is None:
            return _result(spec, FAILED, [f"{name}: no score found in the output"], runs=runs)
        if value < minimum:
            return _result(
                spec, FAILED, [f"{name}: score {value} is below {minimum}"], runs=runs, score=value
            )
        return _result(spec, PASSED, [], runs=runs, score=value)
    return _result(spec, PASSED, [], runs=runs)


def run_stage(
    ci: Mapping[str, Any], stage: str, source: Path, patch: Path, work: Path
) -> dict[str, dict[str, Any]]:
    """Every check of `stage`, in policy order, on a fresh tree under `work`.

    The tree and toolchain are built only if some check is configured. A preparation
    problem marks every configured check `infra_error`.
    """
    specs = stage_checks(ci, stage)
    results: dict[str, dict[str, Any]] = {}
    runnable = [n for n, s in specs.items() if not is_unset(s)]
    tree, venv = work / "tree", work / "venv"
    toolchain = ci["toolchain"]
    uses_venv = toolchain["kind"] == "uv_sync"
    problem: str | None = None
    if runnable:
        try:
            prepare_tree(source, patch, tree)
            if uses_venv:
                prepare_toolchain(tree, venv, toolchain)
        except (CiInfraError, OSError) as exc:
            problem = str(exc)
    for name, spec in specs.items():
        if problem is not None and name in runnable:
            results[name] = _result(spec, INFRA, [f"{name}: {problem}"])
        else:
            results[name] = run_check(
                name, spec, tree, venv if runnable and uses_venv else None, toolchain
            )
    return results
