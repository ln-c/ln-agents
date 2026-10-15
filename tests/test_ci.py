"""CI check execution: tree preparation, unset checks, scores, timeouts, and the toolchain."""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any

import pytest
from sandbox_env import BWRAP_USABLE

from agentic_pipeline import ci
from agentic_pipeline.graph import load_yaml
from agentic_pipeline.policies import check_ci

ROOT = Path(__file__).resolve().parents[1]
ROUTING = load_yaml(ROOT / "policy" / "routing.yaml")
needs_sandbox = pytest.mark.skipif(not BWRAP_USABLE, reason="bwrap unavailable or unusable")
TOOLCHAIN: dict[str, Any] = {"kind": "none", "env": {}, "timeout_seconds": 30}


def test_the_sandbox_is_usable_on_ci() -> None:
    """On CI the sandbox tests must run, not skip: a runner that cannot sandbox is a failure."""
    if os.environ.get("CI"):
        assert BWRAP_USABLE, "bwrap is unusable on this CI runner; fix the workflow step"


def spec(**over: Any) -> dict[str, Any]:
    base = {"stage": "F1", "required": True, "cause": "unknown_or_spent", "commands": []}
    return {**base, **over}


def py(code: str) -> list[list[str]]:
    return [["python3", "-c", code]]


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    path = tmp_path / "tree"
    path.mkdir()
    (path / "marker.txt").write_text("source\n")
    return path


def test_the_patch_is_laid_over_a_copy_of_the_source(tmp_path: Path) -> None:
    source, patch, dest = tmp_path / "s", tmp_path / "p", tmp_path / "d"
    (source / "src").mkdir(parents=True)
    (source / "src" / "a.py").write_text("old\n")
    (source / "keep.txt").write_text("kept\n")
    (patch / "src").mkdir(parents=True)
    (patch / "src" / "a.py").write_text("new\n")
    (patch / "src" / "b.py").write_text("added\n")
    ci.prepare_tree(source, patch, dest)
    assert (dest / "src" / "a.py").read_text() == "new\n"
    assert (dest / "src" / "b.py").read_text() == "added\n"
    assert (dest / "keep.txt").read_text() == "kept\n"
    assert (source / "src" / "a.py").read_text() == "old\n"  # the sealed source is untouched


@pytest.mark.parametrize(
    ("overrides", "required", "status"),
    [
        ({}, True, ci.UNEVALUATED),
        ({}, False, ci.SKIPPED),
        ({"commands": py("pass"), "score": {}}, True, ci.UNEVALUATED),
        ({"commands": py("pass"), "score": {"pattern": "(1)"}}, True, ci.UNEVALUATED),
        ({"commands": py("pass"), "score": {"minimum": 1}}, False, ci.SKIPPED),
    ],
)
def test_an_unset_check_never_passes(
    tree: Path, overrides: dict[str, Any], required: bool, status: str
) -> None:
    result = ci.run_check("mcdc", spec(required=required, **overrides), tree, None, TOOLCHAIN)
    assert result["status"] == status and "runs" not in result  # nothing was executed


@needs_sandbox
def test_commands_run_in_order_in_the_tree_and_stop_at_the_first_failure(tree: Path) -> None:
    commands = [
        *py("import pathlib; pathlib.Path('ran1').write_text('x')"),
        *py("import sys; sys.exit(2)"),
        *py("import pathlib; pathlib.Path('ran3').write_text('x')"),
    ]
    result = ci.run_check("integration", spec(commands=commands), tree, None, TOOLCHAIN)
    assert result["status"] == ci.FAILED and [r["exit_code"] for r in result["runs"]] == [0, 2]
    assert (tree / "ran1").exists() and not (tree / "ran3").exists()


@needs_sandbox
def test_a_check_cannot_reach_the_network_or_the_host_home(tree: Path) -> None:
    code = (
        "import socket, sys; s = socket.socket(); s.settimeout(2)\n"
        "try:\n s.connect(('1.1.1.1', 80)); sys.exit(0)\n"
        "except OSError:\n sys.exit(5)\n"
    )
    result = ci.run_check("integration", spec(commands=py(code)), tree, None, TOOLCHAIN)
    assert result["status"] == ci.FAILED and result["runs"][0]["exit_code"] == 5


@needs_sandbox
def test_a_timeout_is_a_failure(tree: Path) -> None:
    slow = spec(commands=py("import time; time.sleep(30)"), timeout_seconds=1)
    result = ci.run_check("integration", slow, tree, None, TOOLCHAIN)
    assert result["status"] == ci.FAILED and result["runs"][0]["timed_out"] is True


def test_a_uv_managed_interpreter_outside_usr_is_bound_where_the_venv_names_it(
    tmp_path: Path,
) -> None:
    real = tmp_path / "uv" / "cpython-3.14.1-linux"
    (real / "bin").mkdir(parents=True)
    (real / "bin" / "python3.14").write_text("")
    alias = tmp_path / "uv" / "cpython-3.14-linux"  # uv's alias for the newest patch release
    alias.symlink_to(real)
    (tmp_path / "venv" / "bin").mkdir(parents=True)
    (tmp_path / "venv" / "bin" / "python").symlink_to(alias / "bin" / "python3.14")
    assert ci.interpreter_binds(tmp_path / "venv") == {str(alias): real}
    (tmp_path / "bare" / "bin").mkdir(parents=True)
    assert ci.interpreter_binds(tmp_path / "bare") == {}  # no interpreter: nothing to bind
    (tmp_path / "gone" / "bin").mkdir(parents=True)
    (tmp_path / "gone" / "bin" / "python").symlink_to(tmp_path / "missing" / "python")
    assert ci.interpreter_binds(tmp_path / "gone") == {}  # dangling: nothing to bind


@needs_sandbox
@pytest.mark.parametrize(
    ("printed", "status"),
    [
        ("score 91.5", ci.PASSED),
        ("score 80", ci.PASSED),
        ("score 79.9", ci.FAILED),
        ("n/a", ci.FAILED),
    ],
)
def test_the_score_is_read_from_the_last_command_and_compared_to_the_minimum(
    tree: Path, printed: str, status: str
) -> None:
    check = spec(
        commands=py(f"print({printed!r})"), score={"pattern": r"score ([0-9.]+)", "minimum": 80}
    )
    result = ci.run_check("mutation", check, tree, None, TOOLCHAIN)
    assert result["status"] == status


def test_a_missing_lockfile_is_an_infra_problem_for_every_configured_check(
    tmp_path: Path,
) -> None:
    source, patch = tmp_path / "s", tmp_path / "p"
    source.mkdir()
    patch.mkdir()
    policy = {
        "toolchain": {"kind": "uv_sync", "offline": True, "env": {}},
        "checks": {"a": spec(stage="E2", commands=py("pass")), "b": spec(stage="E2")},
    }
    results = ci.run_stage(policy, "E2", source, patch, tmp_path / "work")
    assert results["a"]["status"] == ci.INFRA and "uv.lock" in results["a"]["problems"][0]
    assert results["b"]["status"] == ci.UNEVALUATED  # unset: never executed, never infra


@needs_sandbox
@pytest.mark.skipif(shutil.which("uv") is None, reason="needs uv")
def test_the_pinned_toolchain_is_built_from_the_trees_lockfile_and_bound_into_the_sandbox(
    tmp_path: Path,
) -> None:
    tree = tmp_path / "tree"
    tree.mkdir()
    for name in ("pyproject.toml", "uv.lock"):
        shutil.copy(ROOT / name, tree / name)
    (tree / "src").mkdir()
    venv = tmp_path / "venv"
    try:
        ci.prepare_toolchain(tree, venv, {"offline": False})
    except ci.CiInfraError as exc:  # the local uv cache may lack wheels on a fresh machine
        pytest.skip(str(exc))
    check = spec(commands=[["python", "-c", "import pytest, yaml; print('locked')"]])
    result = ci.run_check("integration", check, tree, venv, TOOLCHAIN)
    assert result["status"] == ci.PASSED, result
    assert "locked" in result["runs"][0]["tail"]


# ── policy/ci.yaml ───────────────────────────────────────────────────────────


def shipped() -> dict[str, Any]:
    return load_yaml(ROOT / "policy" / "ci.yaml")


def test_the_shipped_ci_policy_is_valid_and_leaves_mutation_and_mcdc_unset() -> None:
    doc = shipped()
    assert check_ci(doc, ROUTING) == []
    for name in ("mutation", "mcdc"):
        assert doc["checks"][name]["required"] is True and ci.is_unset(doc["checks"][name])
    # MC/DC and dead code judge the implementation, so they run at E2; mutation testing at F1.
    stages = {n: s["stage"] for n, s in doc["checks"].items()}
    assert (stages["mcdc"], stages["dead_code"], stages["mutation"]) == ("E2", "E2", "F1")
    assert doc["checks"]["dead_code"]["required"] is False
    assert {s["stage"] for s in doc["checks"].values()} == {"E2", "F1"}


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d["checks"].pop("mcdc"),
        lambda d: d["checks"].update(extra=spec()),
        lambda d: d["checks"]["mcdc"].update(stage="X"),
        lambda d: d["checks"]["mcdc"].update(required="yes"),
        lambda d: d["checks"]["mcdc"].update(cause="not_a_route"),
        lambda d: d["checks"]["mcdc"].update(commands=["pytest"]),
        lambda d: d["checks"]["mcdc"].update(commands=[[]]),
        lambda d: d["checks"]["mcdc"].update(score={"pattern": "(a)(b)", "minimum": 1}),
        lambda d: d["checks"]["mcdc"].update(score={"pattern": "(", "minimum": 1}),
        lambda d: d["checks"]["mcdc"].update(score={"minimum": "high"}),
        lambda d: d["checks"]["mcdc"].update(score={"other": 1}),
        lambda d: d["toolchain"].update(kind="pip"),
    ],
)
def test_malformed_ci_policy_is_rejected(mutate: Any) -> None:
    doc = shipped()
    mutate(doc)
    assert check_ci(doc, ROUTING)
