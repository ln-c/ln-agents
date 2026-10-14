"""Build-side gates (E2, X, E3) against sealed artifacts from a real grounding run."""

from __future__ import annotations

import dataclasses
import json
from pathlib import Path
from typing import Any

import pytest
from test_grounding import Rig, repo  # noqa: F401  (repo is a fixture)

from agentic_pipeline.build_gates import (
    BEHAVIORAL,
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


class Build:
    """Grounding walked to G4, then an E1 patch published by hand."""

    def __init__(self, repo: Path, tmp_path: Path) -> None:  # noqa: F811
        self.rig = Rig(repo, tmp_path)
        self.runner: Runner = self.rig.runner()
        for _ in range(4):
            assert self.runner.step().outcome is Outcome.ADVANCED
        GROUNDING_HANDLERS["G4"](self.rig.ctx(self.runner, "G4"))

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
                    "backend": "qwen_local",
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


@pytest.fixture
def build(repo: Path, tmp_path: Path) -> Build:  # noqa: F811
    return Build(repo, tmp_path)


CLEAN = {"src/agentic_pipeline/new.py": "def hello() -> str:\n    return 'hi'\n"}


def statuses(build: Build) -> dict[str, str]:
    return {n: c["status"] for n, c in run_checks(build.ctx("E2"))[1].items()}


def test_clean_patch_passes_every_runnable_check_and_fails_closed_on_the_rest(build: Build) -> None:
    build.publish(CLEAN)
    result = statuses(build)
    assert {n for n, s in result.items() if s == "passed"} == {
        "write_scope",
        "protected_tests_intact",
        "contracts_intact",
        "no_secrets",
        "no_dependency_change",
    }
    assert {n for n, s in result.items() if s == UNEVALUATED} == set(BEHAVIORAL)
    assert BUILD_GATE_HANDLERS["E2"](build.ctx("E2")) == "any_fail_or_exception"


def test_e2_records_an_audit_bound_to_the_patch(build: Build) -> None:
    build.publish(CLEAN)
    BUILD_GATE_HANDLERS["E2"](build.ctx("E2"))
    (record,) = (build.rig.run_dir / "gates" / "E2").glob("*.json")
    doc = json.loads(record.read_text())
    assert doc["patch"].startswith("E1:sha256:") and doc["guard"] == "any_fail_or_exception"
    build.publish({**CLEAN, "src/other.py": "x = 1\n"})
    BUILD_GATE_HANDLERS["E2"](build.ctx("E2"))
    assert len(list((build.rig.run_dir / "gates" / "E2").glob("*.json"))) == 2


@pytest.mark.parametrize(
    ("files", "check"),
    [
        ({"tests/test_x.py": "def test_x(): assert True\n"}, "write_scope"),
        ({"policy/budgets.yaml": "x: 1\n"}, "write_scope"),
        ({"docs/adr/0009-new.md": "# adr\n"}, "write_scope"),
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
    (build.rig.run_dir / "grounding" / "G1" / "protected_tests" / "test_x.py").write_text("evil\n")
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
    (build.rig.run_dir / "manifests" / "G1.json").unlink()
    with pytest.raises(BuildGateError, match="no published manifest"):
        run_checks(build.ctx("E2"))


def test_the_newest_patch_wins(build: Build) -> None:
    build.publish({"tests/test_x.py": "bad\n"}, stage="E1")
    assert statuses(build)["write_scope"] == "failed"
    build.publish(CLEAN, stage="E4")
    assert run_checks(build.ctx("E2"))[0].startswith("E4:")
    assert statuses(build)["write_scope"] == "passed"


def test_x_routes_a_check_that_could_not_run_to_a_human_not_a_retry(build: Build) -> None:
    build.publish(CLEAN)
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


def test_x_classifies_a_failed_behavioral_check_as_a_contract_violation() -> None:
    checks = {"write_scope": {"status": "passed"}, "contract_tests": {"status": "failed"}}
    assert classify(checks) == "implementation_violates_contract"
    # Security wins over a behavioral failure; unknown is the default.
    assert classify({**checks, "no_secrets": {"status": "failed"}}) == "security_or_scope"
    assert classify({"contract_tests": {"status": UNEVALUATED}}) == "unknown_or_spent"


def test_x_only_emits_causes_that_policy_defines(build: Build) -> None:
    defined = set(load_policy(build.rig.repo).routing["routes"])
    assert {cause for cause, _ in CAUSES} | {UNKNOWN_CAUSE} <= defined


def test_x_refuses_to_route_from_anywhere_but_e2(build: Build) -> None:
    build.publish(CLEAN)
    for origin in ("F1", None):
        with pytest.raises(BuildGateError, match="only E2 failures"):
            BUILD_GATE_HANDLERS["X"](build.ctx("X", origin))


def test_x_spent_routes_are_unambiguous(build: Build) -> None:
    graph = build.runner.graph
    assert spent_route(graph, "X", "E3").guard == "local_spent"
    assert spent_route(graph, "X", "H1").guard == "unknown_or_spent"


def test_the_runner_takes_e2_to_x_to_a_human(build: Build) -> None:
    build.publish(CLEAN)
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
    assert result.outcome is Outcome.BLOCKED and "only E2 failures" in result.detail


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
            operations={operation: {"backend": "qwen_local", "invocation_id": "i", "status": "ok"}},
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
