"""Tests for stage manifests and sealed-input verification."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import pytest

from agentic_pipeline.graph import load_yaml
from agentic_pipeline.manifest import (
    ManifestError,
    build_manifest,
    digest_path,
    load_manifest,
    manifest_path,
    publish_stage_manifest,
    verify_sealed_inputs,
)
from agentic_pipeline.provenance import GENESIS_HASH

ROOT = Path(__file__).resolve().parents[1]
REGISTRY = load_yaml(ROOT / "policy" / "model_registry.yaml")


def _artifact(run_root: Path, name: str, text: str = "x\n") -> Path:
    path = run_root / "store" / name
    path.mkdir(parents=True)
    (path / "a.txt").write_text(text)
    return path


def _publish(run_root: Path, stage: str, artifacts: dict[str, tuple[Path, bool]]) -> Path:
    manifest = build_manifest(
        run_root,
        run_id="run-1",
        stage=stage,
        attempt=f"{stage}-1",
        artifacts=artifacts,
        operations={},
        provenance_tip=GENESIS_HASH,
    )
    return publish_stage_manifest(run_root, manifest)


# ── Digests ──────────────────────────────────────────────────────────────────


def test_tree_digest_is_content_addressed(tmp_path: Path) -> None:
    a, b = tmp_path / "a", tmp_path / "b"
    for root in (a, b):
        (root / "sub").mkdir(parents=True)
        (root / "sub" / "f.txt").write_text("same")
    assert digest_path(a) == digest_path(b)
    (b / "sub" / "f.txt").write_text("different")
    assert digest_path(a) != digest_path(b)


def test_tree_digest_detects_renames(tmp_path: Path) -> None:
    (tmp_path / "f.txt").write_text("same")
    before = digest_path(tmp_path)
    (tmp_path / "f.txt").rename(tmp_path / "g.txt")
    assert digest_path(tmp_path) != before


def test_digest_refuses_symlinks(tmp_path: Path) -> None:
    (tmp_path / "real.txt").write_text("x")
    os.symlink(tmp_path / "real.txt", tmp_path / "link.txt")
    with pytest.raises(ManifestError, match="regular files"):
        digest_path(tmp_path)


def test_digest_missing_path(tmp_path: Path) -> None:
    with pytest.raises(ManifestError, match="does not exist"):
        digest_path(tmp_path / "nope")


# ── Publication ──────────────────────────────────────────────────────────────


def test_publish_is_atomic_and_leaves_no_temp_files(tmp_path: Path) -> None:
    path = _publish(tmp_path, "G", {"pinned_context_manifest": (_artifact(tmp_path, "m"), True)})
    assert path == manifest_path(tmp_path, "G")
    assert [p.name for p in path.parent.iterdir()] == ["G.json"]
    assert load_manifest(tmp_path, "G", "run-1")["artifacts"]["pinned_context_manifest"][
        "path"
    ] == ("store/m")


def test_republish_replaces_previous_manifest(tmp_path: Path) -> None:
    _publish(tmp_path, "G", {"pinned_context_manifest": (_artifact(tmp_path, "m1"), True)})
    _publish(tmp_path, "G", {"pinned_context_manifest": (_artifact(tmp_path, "m2"), True)})
    doc = load_manifest(tmp_path, "G", "run-1")
    assert doc["artifacts"]["pinned_context_manifest"]["path"] == "store/m2"


def test_artifact_outside_run_root_rejected(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    with pytest.raises(ManifestError, match="outside the run root"):
        _publish(tmp_path / "run", "G", {"pinned_context_manifest": (outside, True)})


def test_invalid_manifest_not_published(tmp_path: Path) -> None:
    with pytest.raises(ManifestError, match="invalid stage manifest"):
        publish_stage_manifest(tmp_path, {"stage": "G"})
    assert not manifest_path(tmp_path, "G").exists()


@pytest.mark.parametrize("stage", ["../G", "g", ""])
def test_manifest_path_rejects_bad_stage(tmp_path: Path, stage: str) -> None:
    with pytest.raises(ValueError):
        manifest_path(tmp_path, stage)


# ── Loading ──────────────────────────────────────────────────────────────────


def _write_raw(tmp_path: Path, stage: str, doc: Any) -> None:
    path = manifest_path(tmp_path, stage)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(doc if isinstance(doc, str) else json.dumps(doc))


def test_unpublished_manifest_is_none(tmp_path: Path) -> None:
    assert load_manifest(tmp_path, "G", "run-1") is None


def test_corrupt_manifest_raises(tmp_path: Path) -> None:
    _write_raw(tmp_path, "G", '{"partial":')
    with pytest.raises(ManifestError, match="unreadable"):
        load_manifest(tmp_path, "G", "run-1")


def test_manifest_from_other_run_rejected(tmp_path: Path) -> None:
    _publish(tmp_path, "G", {"pinned_context_manifest": (_artifact(tmp_path, "m"), True)})
    with pytest.raises(ManifestError, match="belongs to run-1"):
        load_manifest(tmp_path, "G", "run-2")


def test_path_traversal_in_manifest_rejected(tmp_path: Path) -> None:
    _publish(tmp_path, "G", {"pinned_context_manifest": (_artifact(tmp_path, "m"), True)})
    doc = json.loads(manifest_path(tmp_path, "G").read_text())
    doc["artifacts"]["pinned_context_manifest"]["path"] = "../../etc"
    _write_raw(tmp_path, "G", doc)
    with pytest.raises(ManifestError, match="invalid manifest"):
        load_manifest(tmp_path, "G", "run-1")


# ── Sealed-input verification ────────────────────────────────────────────────


def test_verified_input_passes(tmp_path: Path) -> None:
    _publish(tmp_path, "G", {"pinned_context_manifest": (_artifact(tmp_path, "m"), True)})
    assert verify_sealed_inputs(tmp_path, "run-1", REGISTRY, ["pinned_context_manifest"]) == []


def test_unsealed_own_scratch_not_checked(tmp_path: Path) -> None:
    assert verify_sealed_inputs(tmp_path, "run-1", REGISTRY, ["proposal_a_scratch"]) == []


@pytest.mark.parametrize(
    ("setup", "message"),
    [
        ("none", "no published manifest from ['G']"),
        ("modified", "content changed since G sealed it"),
        ("deleted", "does not exist"),
        ("not_sealed", "does not mark it sealed"),
    ],
)
def test_verification_failures(tmp_path: Path, setup: str, message: str) -> None:
    if setup != "none":
        path = _artifact(tmp_path, "m")
        _publish(tmp_path, "G", {"pinned_context_manifest": (path, setup != "not_sealed")})
        if setup == "modified":
            (path / "a.txt").write_text("changed\n")
        if setup == "deleted":
            (path / "a.txt").unlink()
            path.rmdir()
    errors = verify_sealed_inputs(tmp_path, "run-1", REGISTRY, ["pinned_context_manifest"])
    assert any(message in e for e in errors), errors


def test_artifact_published_by_two_producers_rejected(tmp_path: Path) -> None:
    registry = json.loads(json.dumps(REGISTRY))
    registry["artifacts"]["design_decision_sealed"]["producer"] = ["DR", "B2"]
    for stage in ("DR", "B2"):
        _publish(tmp_path, stage, {"design_decision_sealed": (_artifact(tmp_path, stage), True)})
    errors = verify_sealed_inputs(tmp_path, "run-1", registry, ["design_decision_sealed"])
    assert any("published by several stages" in e for e in errors)
