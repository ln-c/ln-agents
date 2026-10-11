"""Tests for the `validate` command line."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest
import yaml

from agentic_pipeline.cli import main

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def repo_copy(tmp_path: Path) -> Path:
    for name in ("pipeline", "policy", "schemas", "agents"):
        shutil.copytree(ROOT / name, tmp_path / name)
    return tmp_path


def test_validate_ok(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["validate"]) == 0
    assert "repository validation OK (graph, policies, model registry)" in capsys.readouterr().out


def test_validate_reports_registry_violation(
    repo_copy: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    path = repo_copy / "policy" / "model_registry.yaml"
    registry = yaml.safe_load(path.read_text(encoding="utf-8"))
    del registry["stage_requirements"]["BENCH"]
    path.write_text(yaml.safe_dump(registry), encoding="utf-8")

    assert main(["validate", "--root", str(repo_copy)]) == 1
    assert "ERROR: registry R4 BENCH" in capsys.readouterr().err


def test_validate_reports_missing_registry(
    repo_copy: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    (repo_copy / "policy" / "model_registry.yaml").unlink()

    assert main(["validate", "--root", str(repo_copy)]) == 1
    assert "ERROR: missing file: policy/model_registry.yaml" in capsys.readouterr().err
