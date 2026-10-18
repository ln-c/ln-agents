"""Build-side gates (E2, X, E3) against sealed artifacts from a real grounding run."""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import Any

import pytest
from sandbox_env import run_on_host
from test_grounding import Rig, repo  # noqa: F401  (repo is a fixture)

from agentic_pipeline.build_gates import (
    BUILD_GATE_HANDLERS,
    CAUSES,
    UNKNOWN_CAUSE,
    BuildGateError,
    classify,
    run_checks,
)
from agentic_pipeline.gates import UNEVALUATED
from agentic_pipeline.graph import spent_route
from agentic_pipeline.grounding import GROUNDING_HANDLERS
from agentic_pipeline.manifest import build_manifest, publish_stage_manifest
from agentic_pipeline.provenance import GENESIS_HASH
from agentic_pipeline.run_state import load_policy
from agentic_pipeline.runner import HandlerContext, Outcome, Runner

TEST_CI = """version: 1
toolchain: {kind: none, offline: true, env: {}, timeout_seconds: 60}
checks:
  contract_tests:
    {stage: E2, required: false, cause: implementation_violates_contract, commands: []}
  acceptance_tests:
    stage: E2
    required: true
    cause: implementation_violates_contract
    commands: [[python3, -c, "import sys; sys.exit(0)"]]
  performance: {stage: E2, required: false, cause: implementation_violates_contract, commands: []}
  dead_code: {stage: E2, required: false, cause: implementation_violates_contract, commands: []}
  mcdc:
    stage: E2
    required: true
    cause: implementation_violates_contract
    commands: [[python3, -c, "print('mc/dc 100')"]]
    score: {pattern: "mc/dc ([0-9.]+)", minimum: 100}
  integration:
    stage: F1
    required: true
    cause: implementation_violates_contract
    commands: [[python3, -c, "import sys; sys.exit(0)"]]
  mutation: {stage: F1, required: true, cause: requirement_missing, commands: [], score: {}}
"""


class Build:
    """Grounding walked to G, then an E1 patch published by hand.

    The run's CI policy is a small one that needs no uv environment: acceptance_tests and
    mcdc and integration pass, and mutation is unset and required, as in the shipped policy.
    """

    def __init__(self, repo: Path, tmp_path: Path, ci: str = TEST_CI) -> None:  # noqa: F811
        (repo / "policy" / "ci.yaml").write_text(ci)
        self.rig = Rig(repo, tmp_path)
        self.runner: Runner = self.rig.runner()
        assert self.runner.step().outcome is Outcome.ADVANCED  # START
        GROUNDING_HANDLERS["G"](self.rig.ctx(self.runner, "G"))

    def publish(self, files: dict[str, str], stage: str = "E1") -> Path:
        root = self.rig.run_dir / "attempts" / f"{stage}-t" / "build" / "implementation_patch"
        root.mkdir(parents=True, exist_ok=True)
        for old in root.rglob("*"):
            if old.is_file():
                old.unlink()
        for rel, text in files.items():
            (root / rel).parent.mkdir(parents=True, exist_ok=True)
            (root / rel).write_text(text)
        manifest = build_manifest(
            self.rig.run_dir,
            run_id="demo",
            stage=stage,
            attempt="t",
            artifacts={"implementation_patch": (root, False)},
            operations={
                "implementation_patch": {
                    "backend": "qwen3_5_9b",
                    "invocation_id": "i",
                    "status": "ok",
                }
            },
            provenance_tip=GENESIS_HASH,
            data_classes={"implementation_patch": "confidential"},
        )
        publish_stage_manifest(self.rig.run_dir, manifest)
        return root

    def ctx(self, node: str, origin: str | None = "E2") -> HandlerContext:
        return dataclasses.replace(self.rig.ctx(self.runner, node), entered_from=lambda: origin)


@pytest.fixture(autouse=True)
def host_processes(monkeypatch: pytest.MonkeyPatch) -> None:
    """These tests cover gate logic, so checks run as host processes; tests/test_ci.py runs
    the real sandbox (and skips where bwrap is unusable, as on the hosted CI runner)."""
    from agentic_pipeline import ci

    monkeypatch.setattr(ci, "run_sandboxed", run_on_host)


@pytest.fixture
def build(repo: Path, tmp_path: Path) -> Build:  # noqa: F811
    return Build(repo, tmp_path)


CLEAN = {"src/agentic_pipeline/new.py": "def hello() -> str:\n    return 'hi'\n"}


def statuses(build: Build) -> dict[str, str]:
    return {n: c["status"] for n, c in run_checks(build.ctx("E2"))[1].items()}


def test_clean_patch_passes_static_and_configured_checks_and_skips_optional_unset(
    build: Build,
) -> None:
    build.publish(CLEAN)
    assert statuses(build) == {
        "write_scope": "passed",
        "protected_tests_intact": "passed",
        "contracts_intact": "passed",
        "no_secrets": "passed",
        "no_dependency_change": "passed",
        "contract_tests": "skipped",
        "acceptance_tests": "passed",
        "performance": "skipped",
        "dead_code": "skipped",
        "mcdc": "passed",
    }
    assert BUILD_GATE_HANDLERS["E2"](build.ctx("E2")) == "all_pass"


def test_no_behavioral_check_runs_for_a_patch_that_fails_a_static_check(build: Build) -> None:
    build.publish({"tests/test_x.py": "x\n"})
    result = run_checks(build.ctx("E2"))[1]
    assert result["write_scope"]["status"] == "failed"
    for name in ("contract_tests", "acceptance_tests", "performance", "dead_code", "mcdc"):
        assert result[name]["status"] == UNEVALUATED
        assert result[name]["problems"] == ["not run: a static check failed"]
    assert BUILD_GATE_HANDLERS["E2"](build.ctx("E2")) == "any_fail_or_exception"


def test_e2_records_an_audit_bound_to_the_patch(build: Build) -> None:
    build.publish(CLEAN)
    BUILD_GATE_HANDLERS["E2"](build.ctx("E2"))
    (record,) = (build.rig.run_dir / "gates" / "E2").glob("*.json")
    doc = json.loads(record.read_text())
    assert doc["patch"].startswith("E1:sha256:") and doc["guard"] == "all_pass"
    build.publish({**CLEAN, "src/other.py": "x = 1\n"})
    BUILD_GATE_HANDLERS["E2"](build.ctx("E2"))
    assert len(list((build.rig.run_dir / "gates" / "E2").glob("*.json"))) == 2


@pytest.mark.parametrize(
    ("files", "check"),
    [
        ({"tests/test_x.py": "def test_x(): assert True\n"}, "write_scope"),
        ({"policy/budgets.yaml": "x: 1\n"}, "write_scope"),
        ({"scripts/ci_mcdc.py": "x = 1\n"}, "write_scope"),
        ({"contracts/api.json": "{}\n"}, "write_scope"),
        ({".github/CODEOWNERS": "* @me\n"}, "write_scope"),
        ({"src/CODEOWNERS": "* @me\n"}, "write_scope"),
        ({"src/a.py": "KEY = '-----BEGIN RSA PRIVATE KEY-----'\n"}, "no_secrets"),
        ({"src/a.py": "api_key = 'abcd1234abcd1234abcd'\n"}, "no_secrets"),
        ({"pyproject.toml": "[project]\n"}, "no_dependency_change"),
        ({"uv.lock": "version = 1\n"}, "no_dependency_change"),
    ],
)
def test_each_file_check_fails_on_its_violation(
    build: Build, files: dict[str, str], check: str
) -> None:
    build.publish(files)
    assert statuses(build)[check] == "failed"
    assert BUILD_GATE_HANDLERS["E2"](build.ctx("E2")) == "any_fail_or_exception"


def test_tampered_protected_tests_fail_the_check(build: Build) -> None:
    build.publish(CLEAN)
    (build.rig.run_dir / "grounding" / "G" / "protected_tests" / "test_x.py").write_text("evil\n")
    assert statuses(build)["protected_tests_intact"] == "failed"
    assert BUILD_GATE_HANDLERS["X"](build.ctx("X")) == "security_or_scope"


def test_tampered_patch_or_missing_inputs_block(build: Build) -> None:
    with pytest.raises(BuildGateError, match="no implementation patch"):
        run_checks(build.ctx("E2"))
    root = build.publish(CLEAN)
    (root / "src" / "agentic_pipeline" / "new.py").write_text("changed\n")
    with pytest.raises(BuildGateError, match="changed since"):
        run_checks(build.ctx("E2"))
    build.publish(CLEAN)
    (build.rig.run_dir / "manifests" / "G.json").unlink()
    with pytest.raises(BuildGateError, match="no published manifest"):
        run_checks(build.ctx("E2"))


def test_the_newest_patch_wins(build: Build) -> None:
    build.publish({"tests/test_x.py": "bad\n"}, stage="E1")
    assert statuses(build)["write_scope"] == "failed"
    build.publish(CLEAN, stage="E4")
    assert run_checks(build.ctx("E2"))[0].startswith("E4:")
    assert statuses(build)["write_scope"] == "passed"


def test_x_sends_a_failure_it_cannot_reproduce_to_a_human_not_a_retry(build: Build) -> None:
    build.publish(CLEAN)  # every check passes on reproduction
    assert BUILD_GATE_HANDLERS["X"](build.ctx("X")) == "unknown_or_spent"


@pytest.mark.parametrize(
    "files",
    [{"tests/test_x.py": "x\n"}, {"src/a.py": "t = 'ghp_" + "a" * 36 + "'\n"}, {"uv.lock": "v\n"}],
)
def test_x_routes_security_failures_to_a_human(build: Build, files: dict[str, str]) -> None:
    build.publish(files)
    assert BUILD_GATE_HANDLERS["X"](build.ctx("X")) == "security_or_scope"
    (record,) = (build.rig.run_dir / "gates" / "X").glob("*.json")
    assert json.loads(record.read_text())["from"] == "E2"


def test_x_classifies_failures_by_security_then_policy_cause_then_infra() -> None:
    cause = "implementation_violates_contract"
    checks = {
        "write_scope": {"status": "passed"},
        "contract_tests": {"status": "failed", "cause": cause},
    }
    assert classify(checks) == cause
    # Security wins over a behavioral failure; a failed check wins over infra.
    assert classify({**checks, "no_secrets": {"status": "failed"}}) == "security_or_scope"
    infra = {"status": "infra_error", "cause": cause}
    assert classify({**checks, "performance": infra}) == cause
    assert classify({"performance": infra}) == "infra_rerun_e2"
    assert classify({"integration": infra}, "F1") == "infra_rerun_f1"
    # A check that never ran is unknown, never a retry.
    assert classify({"contract_tests": {"status": UNEVALUATED, "cause": cause}}) == (
        "unknown_or_spent"
    )


def test_x_only_emits_causes_that_policy_defines(build: Build) -> None:
    defined = set(load_policy(build.rig.repo).routing["routes"])
    assert {cause for cause, _ in CAUSES} | {UNKNOWN_CAUSE} <= defined


def test_x_refuses_to_route_from_anywhere_but_e2_or_f1(build: Build) -> None:
    build.publish(CLEAN)
    for origin in ("H1", None):
        with pytest.raises(BuildGateError, match="only E2 and F1 failures"):
            BUILD_GATE_HANDLERS["X"](build.ctx("X", origin))


def test_x_spent_routes_are_unambiguous(build: Build) -> None:
    graph = build.runner.graph
    assert spent_route(graph, "X", "E3").guard == "local_spent"
    assert spent_route(graph, "X", "H1").guard == "unknown_or_spent"


def test_the_runner_takes_a_failed_e2_to_x_to_a_human(build: Build) -> None:
    build.publish({"policy/budgets.yaml": "x: 1\n"})
    runner = build.runner
    runner.state.current = "E2"
    runner._save()
    runner.log.append({"state": "E2", "event": "enter"})
    first = runner.step()
    assert first.outcome is Outcome.ADVANCED and runner.state.current == "X", first
    second = runner.step()
    assert second.outcome is Outcome.ADVANCED and runner.state.current == "H1", second
    assert runner.state.ledger["consumed"] == {}  # no retry budget was spent


def test_x_reached_without_an_e2_entry_blocks(build: Build) -> None:
    build.publish(CLEAN)
    runner = build.runner
    runner.state.current = "X"
    runner._save()
    result = runner.step()
    assert result.outcome is Outcome.BLOCKED and "only E2 and F1 failures" in result.detail


def seal_build_inputs(build: Build) -> None:
    for stage, artifact, operation in (
        ("C1", "contracts_sealed", "module_decomposition"),
        ("D1", "generated_tests_sealed", "test_generation"),
    ):
        root = build.rig.run_dir / "attempts" / f"{stage}-t" / operation
        root.mkdir(parents=True)
        (root / "out.txt").write_text("x")
        manifest = build_manifest(
            build.rig.run_dir,
            run_id="demo",
            stage=stage,
            attempt="t",
            artifacts={artifact: (root, True)},
            operations={operation: {"backend": "qwen3_5_9b", "invocation_id": "i", "status": "ok"}},
            provenance_tip=GENESIS_HASH,
            data_classes={artifact: "internal"},
        )
        publish_stage_manifest(build.rig.run_dir, manifest)


def record(build: Build) -> dict[str, Any]:
    (path,) = (build.rig.run_dir / "gates" / "E3").glob("*.json")
    return json.loads(path.read_text())


def test_e3_never_escalates_while_e4_is_disabled(build: Build) -> None:
    seal_build_inputs(build)
    assert BUILD_GATE_HANDLERS["E3"](build.ctx("E3")) == "escalation_not_allowed"
    doc = record(build)
    assert doc["e4_enabled"] is False and not doc["unresolved_inputs"]
    # Source and tests are confidential: only the local agent could ever be eligible.
    assert doc["data_class"] == "confidential" and doc["eligible_agents"] == ["local-large"]


def test_e3_treats_unresolvable_inputs_as_unknown_data(build: Build) -> None:
    assert BUILD_GATE_HANDLERS["E3"](build.ctx("E3")) == "escalation_not_allowed"
    doc = record(build)
    assert doc["data_class"] == "unknown" and doc["eligible_agents"] == []
    assert doc["unresolved_inputs"]  # contracts_sealed / generated_tests_sealed do not exist yet


FAILING = [["python3", "-c", "import sys; sys.exit(3)"]]


def failing_ci(stage_check: str, commands: list[list[str]] = FAILING) -> str:
    import yaml

    doc = yaml.safe_load(TEST_CI)
    doc["checks"][stage_check]["commands"] = commands
    return yaml.safe_dump(doc)


def test_a_failing_e2_check_goes_to_x_and_x_picks_the_policy_cause(
    repo: Path,  # noqa: F811
    tmp_path: Path,
) -> None:
    b = Build(repo, tmp_path, failing_ci("acceptance_tests"))
    b.publish(CLEAN)
    assert BUILD_GATE_HANDLERS["E2"](b.ctx("E2")) == "any_fail_or_exception"
    (record,) = (b.rig.run_dir / "gates" / "E2").glob("*.json")
    check = json.loads(record.read_text())["checks"]["acceptance_tests"]
    assert check["status"] == "failed" and check["runs"][0]["exit_code"] == 3
    assert BUILD_GATE_HANDLERS["X"](b.ctx("X")) == "implementation_violates_contract"


def test_f1_needs_every_required_check_and_the_unset_ones_never_pass(build: Build) -> None:
    build.publish(CLEAN)
    assert BUILD_GATE_HANDLERS["F1"](build.ctx("F1")) == "required_check_failed"
    (record,) = (build.rig.run_dir / "gates" / "F1").glob("*.json")
    checks = json.loads(record.read_text())["checks"]
    assert checks["integration"]["status"] == "passed"
    assert checks["mutation"]["status"] == UNEVALUATED
    assert "required but unset" in checks["mutation"]["problems"][0]
    # X reproduces it and sends a check that never ran to a human, not to a retry.
    assert BUILD_GATE_HANDLERS["X"](build.ctx("X", "F1")) == "unknown_or_spent"


def test_f1_passes_when_every_required_check_is_configured_and_passes(
    repo: Path,  # noqa: F811
    tmp_path: Path,
) -> None:
    import yaml

    doc = yaml.safe_load(TEST_CI)
    ok = [["python3", "-c", "print('coverage 91.5%')"]]
    for name in ("mutation", "mcdc"):
        doc["checks"][name].update(
            commands=ok, score={"pattern": r"coverage ([0-9.]+)%", "minimum": 90}
        )
    b = Build(repo, tmp_path, yaml.safe_dump(doc))
    b.publish(CLEAN)
    assert BUILD_GATE_HANDLERS["F1"](b.ctx("F1")) == "all_required_pass"


def test_a_score_below_its_minimum_fails_and_routes_to_requirement_missing(
    repo: Path,  # noqa: F811
    tmp_path: Path,
) -> None:
    import yaml

    doc = yaml.safe_load(TEST_CI)
    low = [["python3", "-c", "print('mutation score: 40')"]]
    doc["checks"]["mutation"].update(
        commands=low, score={"pattern": r"score: ([0-9.]+)", "minimum": 80}
    )
    b = Build(repo, tmp_path, yaml.safe_dump(doc))
    b.publish(CLEAN)
    assert BUILD_GATE_HANDLERS["F1"](b.ctx("F1")) == "required_check_failed"
    assert BUILD_GATE_HANDLERS["X"](b.ctx("X", "F1")) == "requirement_missing"


def test_f1_refuses_to_run_from_anywhere_but_e2_or_x(build: Build) -> None:
    build.publish(CLEAN)
    with pytest.raises(BuildGateError, match="only E2 or X"):
        BUILD_GATE_HANDLERS["F1"](build.ctx("F1", "H1"))


def test_a_sandbox_that_is_unavailable_is_infra_not_a_verdict(
    build: Build, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agentic_pipeline import ci
    from agentic_pipeline.sandbox import SandboxUnavailable

    def broken(spec: Any, broker: Any) -> Any:
        raise SandboxUnavailable("no bwrap")

    monkeypatch.setattr(ci, "run_sandboxed", broken)
    build.publish(CLEAN)
    assert BUILD_GATE_HANDLERS["E2"](build.ctx("E2")) == "any_fail_or_exception"
    assert BUILD_GATE_HANDLERS["X"](build.ctx("X")) == "infra_rerun_e2"


def test_unset_mcdc_blocks_e2_and_x_sends_it_to_a_human(
    repo: Path,  # noqa: F811
    tmp_path: Path,
) -> None:
    import yaml

    doc = yaml.safe_load(TEST_CI)
    doc["checks"]["mcdc"].update(commands=[], score={})  # as shipped: required and unset
    b = Build(repo, tmp_path, yaml.safe_dump(doc))
    b.publish(CLEAN)
    assert BUILD_GATE_HANDLERS["E2"](b.ctx("E2")) == "any_fail_or_exception"
    (record,) = (b.rig.run_dir / "gates" / "E2").glob("*.json")
    assert json.loads(record.read_text())["checks"]["mcdc"]["status"] == UNEVALUATED
    assert BUILD_GATE_HANDLERS["X"](b.ctx("X")) == "unknown_or_spent"


def test_an_mcdc_score_below_its_minimum_returns_the_attempt_to_the_implementation(
    repo: Path,  # noqa: F811
    tmp_path: Path,
) -> None:
    import yaml

    doc = yaml.safe_load(TEST_CI)
    doc["checks"]["mcdc"]["commands"] = [["python3", "-c", "print('mc/dc 75')"]]
    b = Build(repo, tmp_path, yaml.safe_dump(doc))
    b.publish(CLEAN)
    assert BUILD_GATE_HANDLERS["E2"](b.ctx("E2")) == "any_fail_or_exception"
    assert BUILD_GATE_HANDLERS["X"](b.ctx("X")) == "implementation_violates_contract"
