"""Crash-safe stage completion manifests and sealed-input verification.

Run directory layout (all paths relative to the per-run root):

    attempts/<STAGE>-<id>/<workspace_root>   operation outputs; provisional until sealed
    manifests/<STAGE>.json                   the stage's current published manifest

A manifest records each artifact's path and content digest. It is published by
writing a temp file, fsyncing it, `os.replace`-ing it over the final name, and
fsyncing the directory, so readers see either the previous manifest or the new
one, never a partial write. Downstream stages read sealed inputs only through a
manifest, and only after re-hashing the content on disk.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agentic_pipeline.schemas import ValidationError, validate

MANIFEST_VERSION = 1
MANIFESTS_DIR = "manifests"
ATTEMPTS_DIR = "attempts"
STAGE_RE = re.compile(r"[A-Z][A-Z0-9-]*")


class ManifestError(Exception):
    pass


def digest_path(path: Path) -> str:
    """Content digest of a file or directory tree; symlinks and special files are refused."""
    if path.is_symlink():
        raise ManifestError(f"{path}: symlinks are not allowed in sealed artifacts")
    if path.is_file():
        listing: Any = {"type": "file", "sha256": _file_sha256(path)}
    elif path.is_dir():
        entries = []
        for child in sorted(path.rglob("*")):
            if child.is_symlink() or not (child.is_file() or child.is_dir()):
                raise ManifestError(f"{child}: only regular files and directories may be sealed")
            if child.is_file():
                entries.append([child.relative_to(path).as_posix(), _file_sha256(child)])
        listing = {"type": "tree", "entries": entries}
    else:
        raise ManifestError(f"{path}: does not exist")
    data = json.dumps(listing, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def manifest_path(run_root: Path, stage: str) -> Path:
    if not STAGE_RE.fullmatch(stage):
        raise ValueError(f"invalid stage id {stage!r}")
    return run_root / MANIFESTS_DIR / f"{stage}.json"


def _confined(run_root: Path, rel: str) -> Path:
    root = run_root.resolve()
    path = (root / rel).resolve()
    if not path.is_relative_to(root):
        raise ManifestError(f"artifact path {rel!r} escapes the run root")
    return path


def build_manifest(
    run_root: Path,
    *,
    run_id: str,
    stage: str,
    attempt: str,
    artifacts: Mapping[str, tuple[Path, bool]],
    operations: Mapping[str, Mapping[str, str]],
    provenance_tip: str,
) -> dict[str, Any]:
    """Digest each artifact (id -> (path, sealed)) and assemble a manifest."""
    root = run_root.resolve()
    entries = {}
    for artifact_id, (path, sealed) in artifacts.items():
        resolved = path.resolve()
        if not resolved.is_relative_to(root):
            raise ManifestError(f"{artifact_id}: {path} is outside the run root")
        entries[artifact_id] = {
            "path": resolved.relative_to(root).as_posix(),
            "digest": digest_path(path),
            "sealed": sealed,
        }
    return {
        "manifest_version": MANIFEST_VERSION,
        "run_id": run_id,
        "stage": stage,
        "attempt": attempt,
        "published_at": datetime.now(UTC).isoformat(),
        "provenance_tip": provenance_tip,
        "operations": {k: dict(v) for k, v in operations.items()},
        "artifacts": entries,
    }


def publish_stage_manifest(run_root: Path, manifest: Mapping[str, Any]) -> Path:
    """Atomically publish `manifest` as manifests/<stage>.json."""
    try:
        validate("stage_manifest", manifest)
    except ValidationError as exc:
        raise ManifestError(f"invalid stage manifest: {exc.message}") from exc
    final = manifest_path(run_root, manifest["stage"])
    final.parent.mkdir(parents=True, exist_ok=True)
    tmp = final.with_name(f".{final.name}.tmp.{uuid.uuid4().hex}")
    try:
        with tmp.open("x", encoding="utf-8") as fh:
            json.dump(manifest, fh, sort_keys=True, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, final)
    finally:
        tmp.unlink(missing_ok=True)
    fd = os.open(final.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    return final


def load_manifest(run_root: Path, stage: str, run_id: str) -> dict[str, Any] | None:
    """Return the stage's published manifest, None if unpublished; ManifestError if invalid."""
    path = manifest_path(run_root, stage)
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        raise ManifestError(f"{stage}: unreadable manifest: {exc}") from exc
    try:
        validate("stage_manifest", doc)
    except ValidationError as exc:
        raise ManifestError(f"{stage}: invalid manifest: {exc.message}") from exc
    if doc["stage"] != stage or doc["run_id"] != run_id:
        raise ManifestError(f"{stage}: manifest belongs to {doc['run_id']}/{doc['stage']}")
    return doc


def resolve_sealed_inputs(
    run_root: Path, run_id: str, registry: Mapping[str, Any], allowed_reads: list[str]
) -> tuple[dict[str, Path], list[str]]:
    """Locate and re-hash every sealed input; return (artifact_id -> path, errors).

    Each sealed input must be published by exactly one manifest and be unchanged on
    disk. Unsealed reads are an operation's own scratch (registry R10) and are skipped.
    """
    paths: dict[str, Path] = {}
    errors: list[str] = []
    artifacts = registry["artifacts"]
    for artifact_id in allowed_reads:
        spec = artifacts[artifact_id]
        if not spec["sealed"]:
            continue
        stages = sorted({p.split(".")[0] for p in spec["producer"]})
        found: list[tuple[str, Mapping[str, Any]]] = []
        try:
            for stage in stages:
                doc = load_manifest(run_root, stage, run_id)
                if doc is not None and artifact_id in doc["artifacts"]:
                    found.append((stage, doc["artifacts"][artifact_id]))
        except ManifestError as exc:
            errors.append(f"{artifact_id}: {exc}")
            continue
        if not found:
            errors.append(f"{artifact_id}: no published manifest from {stages}")
            continue
        if len(found) > 1:
            errors.append(f"{artifact_id}: published by several stages {[s for s, _ in found]}")
            continue
        stage, entry = found[0]
        if not entry["sealed"]:
            errors.append(f"{artifact_id}: {stage} manifest does not mark it sealed")
            continue
        try:
            path = _confined(run_root, entry["path"])
            actual = digest_path(path)
        except ManifestError as exc:
            errors.append(f"{artifact_id}: {exc}")
            continue
        if actual != entry["digest"]:
            errors.append(f"{artifact_id}: content changed since {stage} sealed it")
            continue
        paths[artifact_id] = path
    return paths, errors


def verify_sealed_inputs(
    run_root: Path, run_id: str, registry: Mapping[str, Any], allowed_reads: list[str]
) -> list[str]:
    return resolve_sealed_inputs(run_root, run_id, registry, allowed_reads)[1]
