"""Grounding handlers (START, G1-G4) against a throwaway git repository."""

from __future__ import annotations

import copy
import json
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from agentic_pipeline.grounding import GROUNDING_HANDLERS, context_hash
from agentic_pipeline.manifest import load_manifest, resolve_sealed_inputs
from agentic_pipeline.provenance import JsonlProvenanceLog
from agentic_pipeline.run_state import RunStateError
from agentic_pipeline.runner import HandlerContext, Outcome, Runner, init_run, open_run
from agentic_pipeline.schemas import ValidationError, validate

ROOT = Path(__file__).resolve().parents[1]
RUN_ID = "demo"
SPEC: dict[str, Any] = {
    "schema_version": "1.1",
    "task": {"objective": "Add a health endpoint", "problem_statement": None, "non_goals": []},
    "classification": {"primary_class": "api_addition", "impact_labels": [], "confidence": "high"},
    "scope": {
        "declared_modules": [],
        "inferred_impacts": [],
        "declared_contracts": [],
        "excluded_scope": [],
        "dependency_constraints": [],
    },
    "guardrails": {
        "required_adrs": [],
        "compatibility_constraints": [],
        "security_constraints": [],
    },
    "data": {
        "known_flows": [],
        "declared_classification": "public",
        "unresolved_classification": [],
    },
    "acceptance": {"criteria": [], "required_tests": [], "quality_gates": []},
    "interview": {"unresolved_questions": [], "assumptions": [], "user_confirmations": []},
    "handoff": {"status": "pending_validation"},
}


def git(repo: Path, *args: str) -> str:
    done = subprocess.run(  # noqa: S603
        ["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *args],  # noqa: S607
        check=True,
        capture_output=True,
        text=True,
    )
    return done.stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    path = tmp_path / "repo"
    path.mkdir()
    for name in ("pipeline", "policy", "schemas", "agents"):
        shutil.copytree(ROOT / name, path / name)
    (path / "docs" / "adr").mkdir(parents=True)
    (path / "docs" / "adr" / "0001-first.md").write_text("# 1\n\nStatus: Accepted\n")
    (path / "tests").mkdir()
    (path / "tests" / "test_x.py").write_text("def test_x(): ...\n")
    git(path, "init", "-q")
    git(path, "add", "-A")
    git(path, "commit", "-q", "-m", "init")
    return path


def write_spec(tmp_path: Path, **patch: Any) -> Path:
    spec = copy.deepcopy(SPEC)
    for dotted, value in patch.items():
        section, key = dotted.split("__")
        spec[section][key] = value
    path = tmp_path / "spec.json"
    path.write_text(json.dumps(spec))
    return path


class Rig:
    def __init__(self, repo: Path, tmp_path: Path, spec: Path | None = None) -> None:
        self.repo = repo
        self.run_dir = tmp_path / "runs" / RUN_ID
        init_run(repo, self.run_dir, RUN_ID, "START", spec if spec else write_spec(tmp_path))

    def runner(self) -> Runner:
        # Grounding never dispatches a model; a stub factory is enough for these steps.
        return open_run(self.repo, self.run_dir, lambda *a: object())  # type: ignore[arg-type,return-value]

    def ctx(self, runner: Runner, node: str) -> HandlerContext:
        return HandlerContext(
            RUN_ID,
            self.run_dir,
            node,
            runner.graph,
            runner.registry,
            root=self.repo,
            policy_digests=runner.state.policy,
            provenance_tip=runner.log.tip,
        )

    def manifest(self, stage: str) -> dict[str, Any] | None:
        return load_manifest(self.run_dir, stage, RUN_ID)


def test_entry_spec_example_validates() -> None:
    validate("entry_spec", SPEC)
    bad = copy.deepcopy(SPEC)
    bad["schema_version"] = "1.0"
    with pytest.raises(ValidationError):
        validate("entry_spec", bad)


def test_init_rejects_an_invalid_entry_spec(repo: Path, tmp_path: Path) -> None:
    spec = tmp_path / "bad.json"
    spec.write_text("{}")
    with pytest.raises(RunStateError, match="entry spec"):
        init_run(repo, tmp_path / "runs" / RUN_ID, RUN_ID, "START", spec)
    assert not (tmp_path / "runs" / RUN_ID).exists()


def test_walk_through_grounding_seals_a_valid_context(repo: Path, tmp_path: Path) -> None:
    rig = Rig(repo, tmp_path)
    runner = rig.runner()
    for expected in ("G1", "G2", "G3", "G4"):
        result = runner.step()
        assert result.outcome is Outcome.ADVANCED and runner.state.current == expected, result
    assert GROUNDING_HANDLERS["G4"](rig.ctx(runner, "G4")) == "always"

    head = git(repo, "rev-parse", "HEAD")
    sealed, errors = resolve_sealed_inputs(
        rig.run_dir,
        RUN_ID,
        runner.registry,
        [
            "requirements",
            "pinned_source_tree",
            "applicable_contracts",
            "adr_snapshot",
            "protected_tests",
            "pinned_context_manifest",
        ],
    )
    assert not errors, errors
    assert {name: s.data_class for name, s in sealed.items()} == {
        "requirements": "internal",
        "pinned_source_tree": "confidential",
        "applicable_contracts": "confidential",
        "adr_snapshot": "internal",
        "protected_tests": "confidential",
        "pinned_context_manifest": "confidential",  # inherits the most restrictive input
    }

    path = sealed["pinned_context_manifest"].path / "context_manifest.json"
    doc = json.loads(path.read_text())
    validate("context_manifest", doc)
    assert doc["commit_sha"] == head
    assert doc["adrs"] == [{"id": "0001-first", "status": "accepted"}]
    assert doc["contract_versions"] == {}
    assert doc["hash"] == context_hash(doc)
    assert (sealed["protected_tests"].path / "test_x.py").is_file()
    assert (sealed["pinned_source_tree"].path / "docs" / "adr" / "0001-first.md").is_file()


def requirements_class(repo: Path, tmp_path: Path, declared: str | None) -> str:
    rig = Rig(repo, tmp_path, write_spec(tmp_path, data__declared_classification=declared))
    runner = rig.runner()
    for _ in range(3):  # START, G1, G2 handlers
        runner.step()
    audit = json.loads((rig.run_dir / "grounding" / "G2" / "classification.json").read_text())
    assert audit["rules"] == "artifact_defaults"
    manifest = rig.manifest("START")
    assert manifest is not None
    assert manifest["artifacts"]["requirements"]["data_class"] == audit["classes"]["requirements"]
    return str(audit["classes"]["requirements"])


@pytest.mark.parametrize(
    ("declared", "expected"),
    [
        (None, "internal"),
        ("public", "internal"),  # a declaration can never lower a class
        ("Internal", "internal"),
        ("Confidential", "confidential"),
        ("restricted", "restricted"),
        ("top secret", "unknown"),  # not a policy class: assume the worst
    ],
)
def test_declared_classification_only_raises_requirements(
    repo: Path, tmp_path: Path, declared: str | None, expected: str
) -> None:
    assert requirements_class(repo, tmp_path, declared) == expected


@pytest.mark.parametrize(
    ("patch", "message"),
    [
        ({"classification__primary_class": "mystery"}, "task_classes"),
        ({"classification__primary_class": None}, "task_classes"),
        ({"handoff__status": "blocked_pending_clarification"}, "pending_validation"),
        ({"task__objective": "   "}, "empty task objective"),
    ],
)
def test_start_blocks_an_unacceptable_spec(
    repo: Path, tmp_path: Path, patch: dict[str, Any], message: str
) -> None:
    rig = Rig(repo, tmp_path, write_spec(tmp_path, **patch))
    runner = rig.runner()
    result = runner.step()
    assert result.outcome is Outcome.BLOCKED and message in result.detail, result
    assert runner.state.current == "START" and rig.manifest("START") is None


def test_start_blocks_without_an_entry_spec(repo: Path, tmp_path: Path) -> None:
    run_dir = tmp_path / "runs" / RUN_ID
    init_run(repo, run_dir, RUN_ID, "START")
    result = open_run(repo, run_dir, lambda *a: object()).step()  # type: ignore[arg-type,return-value]
    assert result.outcome is Outcome.BLOCKED and "no entry spec" in result.detail


def test_g1_does_not_repin_when_head_moves(repo: Path, tmp_path: Path) -> None:
    rig = Rig(repo, tmp_path)
    runner = rig.runner()
    runner.step()  # START
    runner.step()  # G1
    pinned = rig.manifest("G1")
    (repo / "new.txt").write_text("later\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "later")
    assert GROUNDING_HANDLERS["G1"](rig.ctx(runner, "G1")) == "always"
    assert rig.manifest("G1") == pinned
    snapshot = json.loads(
        (rig.run_dir / "grounding" / "G1" / "adr_snapshot" / "snapshot.json").read_text()
    )
    assert snapshot["commit_sha"] != git(repo, "rev-parse", "HEAD")


def test_g1_refuses_to_redo_a_tampered_stage(repo: Path, tmp_path: Path) -> None:
    rig = Rig(repo, tmp_path)
    runner = rig.runner()
    runner.step()
    runner.step()
    (rig.run_dir / "grounding" / "G1" / "protected_tests" / "test_x.py").write_text("evil\n")
    with pytest.raises(Exception, match="changed since it was sealed"):
        GROUNDING_HANDLERS["G1"](rig.ctx(runner, "G1"))


def test_g1_blocks_a_symlink_in_the_pinned_tree(repo: Path, tmp_path: Path) -> None:
    (repo / "link").symlink_to("tests")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "link")
    rig = Rig(repo, tmp_path)
    runner = rig.runner()
    assert runner.step().outcome is Outcome.ADVANCED  # START
    result = runner.step()
    assert result.outcome is Outcome.BLOCKED and "regular files" in result.detail, result
    assert rig.manifest("G1") is None


def test_g1_reads_the_commit_not_the_working_tree(repo: Path, tmp_path: Path) -> None:
    (repo / "tests" / "test_x.py").write_text("dirty = True\n")  # uncommitted
    (repo / "untracked.txt").write_text("x\n")
    rig = Rig(repo, tmp_path)
    runner = rig.runner()
    runner.step()
    runner.step()
    sealed, _ = resolve_sealed_inputs(rig.run_dir, RUN_ID, runner.registry, ["protected_tests"])
    assert (sealed["protected_tests"].path / "test_x.py").read_text() == "def test_x(): ...\n"
    assert not (rig.run_dir / "grounding" / "G1" / "pinned_source_tree" / "untracked.txt").exists()


def test_adr_with_an_unknown_status_blocks_g1(repo: Path, tmp_path: Path) -> None:
    (repo / "docs" / "adr" / "0002-odd.md").write_text("# 2\n\nStatus: Maybe\n")
    git(repo, "add", "-A")
    git(repo, "commit", "-q", "-m", "odd")
    rig = Rig(repo, tmp_path)
    runner = rig.runner()
    runner.step()
    result = runner.step()
    assert result.outcome is Outcome.BLOCKED and "Status must be one of" in result.detail


def test_grounding_refuses_after_policy_drift(repo: Path, tmp_path: Path) -> None:
    rig = Rig(repo, tmp_path)
    runner = rig.runner()
    (repo / "policy" / "task_classes.yaml").write_text(
        (repo / "policy" / "task_classes.yaml").read_text() + "# edited\n"
    )
    with pytest.raises(Exception, match="changed since the run started"):
        GROUNDING_HANDLERS["START"](rig.ctx(runner, "START"))


def test_provenance_tip_in_manifests_is_a_chain_hash(repo: Path, tmp_path: Path) -> None:
    rig = Rig(repo, tmp_path)
    runner = rig.runner()
    runner.step()
    doc = rig.manifest("START")
    assert doc is not None
    hashes = {
        r["event_hash"] for r in JsonlProvenanceLog(rig.run_dir / "provenance", RUN_ID).records()
    }
    assert doc["provenance_tip"] in hashes
