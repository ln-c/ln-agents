"""Deterministic grounding handlers for START and G1-G4.

Run directory layout (relative to the per-run root):

    inputs/entry_spec.json                       copied in by `run init`
    grounding/START/requirements/                sealed copy of the entry spec
    grounding/G1/{pinned_source_tree,applicable_contracts,adr_snapshot,protected_tests}/
                                                 extracted from git at the pinned SHA, sealed;
                                                 adr_snapshot/snapshot.json records the SHA,
                                                 ADR statuses, and contract versions
    grounding/G2/classification.json             audit record of the classification pass
    grounding/G3/file_index.json                 minimal file-level index
    grounding/G4/pinned_context_manifest/        the hashed context manifest (sealed)

Every stage publishes (or, for G2, updates) a stage manifest, which is how downstream
stages find its sealed artifacts. These sit outside every `workspace_roots` path.

Limits, stated plainly:
* G2 classifies by artifact role from `artifact_defaults` in policy/data_classes.yaml
  (code confidential, specs and contracts internal); an artifact missing there is
  `unknown`. The entry spec's `declared_classification` may only RAISE `requirements`:
  a label that is not a policy class counts as `unknown`. There are no path or content
  rules, so one class covers a whole artifact.
* G3 builds a file-level index only. SCIP, Tree-sitter, vector and graph indexes are
  not built; the index file says so.
* `applicable_contracts` is whatever `contracts/` holds at the SHA (empty today).

Handlers are idempotent: a stage whose manifest already verifies is not redone, so a
half-finished run never re-pins a different SHA.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import tarfile
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any

from agentic_pipeline.frozen import PolicyChanged, frozen_policy
from agentic_pipeline.fsutil import atomic_write_json
from agentic_pipeline.manifest import (
    ManifestError,
    build_manifest,
    digest_path,
    load_manifest,
    publish_stage_manifest,
    resolve_sealed_inputs,
)
from agentic_pipeline.run_state import PolicySnapshot
from agentic_pipeline.schemas import ValidationError, validate

if TYPE_CHECKING:
    from agentic_pipeline.runner import HandlerContext

GROUNDING_DIR = "grounding"
INPUTS_DIR = "inputs"
ENTRY_SPEC_FILE = "entry_spec.json"
SNAPSHOT_FILE = "snapshot.json"
MAX_ENTRY_SPEC_BYTES = 1 << 20
ADR_STATUSES = ("proposed", "accepted", "superseded", "deprecated", "implemented")
DEFERRED_INDEXES = ["scip", "tree_sitter", "vector", "graph"]

G1_ARTIFACTS = ("pinned_source_tree", "applicable_contracts", "adr_snapshot", "protected_tests")
# Repo subtree each G1 artifact is taken from (None = the whole tree).
G1_SOURCES: dict[str, str | None] = {
    "pinned_source_tree": None,
    "applicable_contracts": "contracts",
    "adr_snapshot": "docs/adr",
    "protected_tests": "tests",
}
LANGUAGES = {
    ".py": "python",
    ".md": "markdown",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".json": "json",
    ".toml": "toml",
    ".sh": "shell",
    ".js": "javascript",
    ".ts": "typescript",
}


class GroundingError(Exception):
    pass


# ── helpers ──────────────────────────────────────────────────────────────────


def load_entry_spec(data: bytes) -> dict[str, Any]:
    """Parse and schema-validate an entry spec; raises GroundingError."""
    if len(data) > MAX_ENTRY_SPEC_BYTES:
        raise GroundingError(f"entry spec exceeds {MAX_ENTRY_SPEC_BYTES} bytes")
    try:
        doc = json.loads(data)
    except ValueError as exc:
        raise GroundingError(f"entry spec is not valid JSON: {exc}") from exc
    try:
        validate("entry_spec", doc)
    except ValidationError as exc:
        raise GroundingError(f"entry spec invalid: {exc.message}") from exc
    return doc


def _policy(ctx: HandlerContext) -> PolicySnapshot:
    try:
        return frozen_policy(ctx)
    except PolicyChanged as exc:
        raise GroundingError(str(exc)) from exc


def _stage_dir(ctx: HandlerContext, stage: str) -> Path:
    return ctx.run_root / GROUNDING_DIR / stage


def _fresh(path: Path) -> Path:
    shutil.rmtree(path, ignore_errors=True)
    path.mkdir(parents=True)
    return path


def _published(ctx: HandlerContext, stage: str, ids: tuple[str, ...]) -> dict[str, Any] | None:
    """The stage's manifest if published; None if the stage never published.

    A published manifest that is unreadable, differs from `ids`, or no longer matches the
    disk raises: redoing the stage would silently re-pin it (G1 to a HEAD that moved).
    """
    try:
        doc = load_manifest(ctx.run_root, stage, ctx.run_id)
        if doc is None:
            return None
        if set(doc["artifacts"]) != set(ids):
            raise GroundingError(f"{stage}: manifest artifacts are {sorted(doc['artifacts'])}")
        root = ctx.run_root.resolve()
        for artifact, entry in doc["artifacts"].items():
            if digest_path(root / entry["path"]) != entry["digest"]:
                raise GroundingError(f"{stage}.{artifact}: content changed since it was sealed")
    except ManifestError as exc:
        raise GroundingError(f"{stage}: published manifest is unusable: {exc}") from exc
    return doc


def _publish(
    ctx: HandlerContext,
    stage: str,
    artifacts: Mapping[str, Path],
    data_classes: Mapping[str, str] | None = None,
) -> None:
    manifest = build_manifest(
        ctx.run_root,
        run_id=ctx.run_id,
        stage=stage,
        attempt="grounding",
        artifacts={aid: (path, True) for aid, path in artifacts.items()},
        operations={},
        provenance_tip=ctx.provenance_tip(),
        data_classes=data_classes,
    )
    publish_stage_manifest(ctx.run_root, manifest)


def _git(root: Path, *args: str) -> str:
    git = shutil.which("git")
    if git is None:
        raise GroundingError("git is not installed")
    try:
        done = subprocess.run(  # noqa: S603 - fixed read-only git subcommands, no shell
            [git, "-C", str(root), *args], check=True, capture_output=True, text=True
        )
    except (OSError, subprocess.CalledProcessError) as exc:
        detail = getattr(exc, "stderr", "") or exc
        raise GroundingError(f"git {args[0]} failed: {str(detail).strip()}") from exc
    return done.stdout


def _extract(root: Path, sha: str, subdir: str | None, dest: Path) -> None:
    """Materialize `subdir` (or the whole tree) of commit `sha` into `dest`.

    Reads git objects, never the working tree, so content always matches the SHA.
    Anything but regular files and directories is refused (they cannot be sealed).
    """
    dest.mkdir(parents=True)
    if subdir and not _git(root, "ls-tree", sha, "--", subdir).strip():
        return  # absent at this SHA: an empty directory still digests
    tarball = dest.parent / f".{dest.name}.tar"
    try:
        _git(
            root,
            "archive",
            "--format=tar",
            "-o",
            str(tarball),
            f"{sha}:{subdir}" if subdir else sha,
        )
        with tarfile.open(tarball) as tar:
            for member in tar:
                if not (member.isfile() or member.isdir()):
                    raise GroundingError(f"{member.name}: only regular files can be pinned")
                tar.extract(member, dest, filter="data")
    except tarfile.TarError as exc:
        raise GroundingError(f"cannot extract {sha}:{subdir or ''}: {exc}") from exc
    finally:
        tarball.unlink(missing_ok=True)


def _adr_statuses(adr_dir: Path) -> list[dict[str, str]]:
    adrs = []
    for path in sorted(adr_dir.glob("[0-9][0-9][0-9][0-9]-*.md")):
        found = re.search(r"^Status:\s*(\S+)", path.read_text(encoding="utf-8"), re.MULTILINE)
        status = found.group(1).lower() if found else ""
        if status not in ADR_STATUSES:
            raise GroundingError(f"{path.name}: Status must be one of {list(ADR_STATUSES)}")
        adrs.append({"id": path.stem, "status": status})
    return adrs


def _contract_versions(root: Path, sha: str) -> dict[str, str]:
    """Git blob id of each contract file at the SHA: a content-derived version pin."""
    versions = {}
    for line in _git(root, "ls-tree", "-r", sha, "--", "contracts").splitlines():
        meta, path = line.split("\t", 1)
        versions[path] = meta.split()[2]
    return versions


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ── START ────────────────────────────────────────────────────────────────────


def start(ctx: HandlerContext) -> str:
    """Validate the entry spec against policy and seal it as `requirements`."""
    snap = _policy(ctx)
    if _published(ctx, "START", ("requirements",)):
        return "task_class_valid"
    source = ctx.run_root / INPUTS_DIR / ENTRY_SPEC_FILE
    try:
        data = source.read_bytes()
    except OSError as exc:
        raise GroundingError(f"no entry spec: {exc}") from exc
    spec = load_entry_spec(data)
    if spec["handoff"]["status"] != "pending_validation":
        raise GroundingError(f"entry spec is {spec['handoff']['status']}, not pending_validation")
    if not spec["task"]["objective"].strip():
        raise GroundingError("entry spec has an empty task objective")
    primary = spec["classification"]["primary_class"]
    classes = snap.task_classes.get("classes") or {}
    if primary not in classes:
        raise GroundingError(
            f"task class {primary!r} is not in policy/task_classes.yaml {sorted(classes)}"
        )
    requirements = _fresh(_stage_dir(ctx, "START")) / "requirements"
    requirements.mkdir()
    (requirements / ENTRY_SPEC_FILE).write_bytes(data)
    _publish(ctx, "START", {"requirements": requirements})
    return "task_class_valid"


# ── G1 ───────────────────────────────────────────────────────────────────────


def pin_snapshot(ctx: HandlerContext) -> str:
    """Pin HEAD and seal the source tree, contracts, ADRs, and protected tests at it."""
    _policy(ctx)
    if _published(ctx, "G1", G1_ARTIFACTS):
        return "always"
    sha = _git(ctx.root, "rev-parse", "--verify", "HEAD^{commit}").strip()
    base = _fresh(_stage_dir(ctx, "G1"))
    for artifact, subdir in G1_SOURCES.items():
        _extract(ctx.root, sha, subdir, base / artifact)
    snapshot = {
        "commit_sha": sha,
        "adrs": _adr_statuses(base / "adr_snapshot"),
        "contract_versions": _contract_versions(ctx.root, sha),
    }
    atomic_write_json(base / "adr_snapshot" / SNAPSHOT_FILE, snapshot)
    _publish(ctx, "G1", {artifact: base / artifact for artifact in G1_ARTIFACTS})
    return "always"


# ── G2 ───────────────────────────────────────────────────────────────────────


def classify_artifact(artifact_id: str, policy: PolicySnapshot, declared: str | None = None) -> str:
    """Policy class of one artifact; `declared` (a human label) can only raise it."""
    classes = policy.data_classes["classes"]
    order = policy.data_classes["order"]
    cls = (policy.data_classes.get("artifact_defaults") or {}).get(artifact_id, "unknown")
    if cls not in classes:
        raise GroundingError(
            f"policy/data_classes.yaml: {artifact_id} maps to unknown class {cls!r}"
        )
    if declared is not None:
        label = declared.strip().lower()
        claimed = label if label in classes else "unknown"  # unrecognized: assume the worst
        cls = max(cls, claimed, key=order.index)
    return cls


def classify(ctx: HandlerContext) -> str:
    """Record a data class on every artifact START and G1 published."""
    snap = _policy(ctx)
    stages = {"START": ("requirements",), "G1": G1_ARTIFACTS}
    spec = json.loads((ctx.run_root / INPUTS_DIR / ENTRY_SPEC_FILE).read_bytes())
    declared = spec["data"]["declared_classification"]
    classes = {
        artifact: classify_artifact(
            artifact, snap, declared if artifact == "requirements" else None
        )
        for ids in stages.values()
        for artifact in ids
    }
    audit = _fresh(_stage_dir(ctx, "G2"))
    atomic_write_json(
        audit / "classification.json",
        {"rules": "artifact_defaults", "declared_classification": declared, "classes": classes},
    )
    for stage, ids in stages.items():
        doc = _published(ctx, stage, ids)
        if doc is None:
            raise GroundingError(f"{stage}: manifest missing")
        if all("data_class" in e for e in doc["artifacts"].values()):
            continue
        for artifact in ids:
            doc["artifacts"][artifact]["data_class"] = classes[artifact]
        publish_stage_manifest(ctx.run_root, doc)
    return "always"


# ── G3 ───────────────────────────────────────────────────────────────────────


def build_index(ctx: HandlerContext) -> str:
    """Minimal file-level index of the pinned source tree (not a code-intelligence index)."""
    _policy(ctx)
    inputs, errors = resolve_sealed_inputs(
        ctx.run_root, ctx.run_id, ctx.registry, ["pinned_source_tree"]
    )
    if errors:
        raise GroundingError("; ".join(errors))
    tree = inputs["pinned_source_tree"].path
    files = [
        {
            "path": path.relative_to(tree).as_posix(),
            "sha256": _sha256_file(path),
            "language": LANGUAGES.get(path.suffix, "other"),
        }
        for path in sorted(tree.rglob("*"))
        if path.is_file()
    ]
    out = _stage_dir(ctx, "G3")
    out.mkdir(parents=True, exist_ok=True)
    atomic_write_json(
        out / "file_index.json",
        {"kind": "file_index", "deferred": DEFERRED_INDEXES, "files": files},
    )
    return "always"


# ── G4 ───────────────────────────────────────────────────────────────────────


def _canonical(document: Mapping[str, Any]) -> bytes:
    return json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def context_hash(document: Mapping[str, Any]) -> str:
    """sha256 of the canonical JSON (sorted keys, no spaces) of the document minus `hash`."""
    body = {k: v for k, v in document.items() if k != "hash"}
    return "sha256:" + hashlib.sha256(_canonical(body)).hexdigest()


def write_context(ctx: HandlerContext) -> str:
    """Write the hashed, immutable context manifest and seal it."""
    snap = _policy(ctx)
    if _published(ctx, "G4", ("pinned_context_manifest",)):
        return "always"
    inputs, errors = resolve_sealed_inputs(
        ctx.run_root, ctx.run_id, ctx.registry, ["requirements", *G1_ARTIFACTS]
    )
    if errors:
        raise GroundingError("; ".join(errors))
    if not (_stage_dir(ctx, "G3") / "file_index.json").is_file():
        raise GroundingError("G3 file index is missing")
    snapshot = json.loads((inputs["adr_snapshot"].path / SNAPSHOT_FILE).read_bytes())
    classes = {artifact: item.data_class for artifact, item in inputs.items()}
    document: dict[str, Any] = {
        "run_id": ctx.run_id,
        "parent_run_id": None,
        "commit_sha": snapshot["commit_sha"],
        "adrs": snapshot["adrs"],
        "contract_versions": snapshot["contract_versions"],
        "data_classes": classes,
        "policy_version": "sha256:" + hashlib.sha256(_canonical(snap.digests)).hexdigest(),
        "scorer_version": (
            f"weights-v{snap.weights['version']}:{snap.digests['policy/weights.yaml']}"
        ),
    }
    document["hash"] = context_hash(document)
    try:
        validate("context_manifest", document)
    except ValidationError as exc:
        raise GroundingError(f"context manifest invalid: {exc.message}") from exc
    out = _fresh(_stage_dir(ctx, "G4")) / "pinned_context_manifest"
    out.mkdir()
    atomic_write_json(out / "context_manifest.json", document)
    # Classified by role, not inherited: the schema fixes the manifest's fields (see
    # policy/data_classes.yaml), so it cannot carry the source tree's content.
    label = classify_artifact("pinned_context_manifest", snap)
    _publish(ctx, "G4", {"pinned_context_manifest": out}, {"pinned_context_manifest": label})
    return "always"


GROUNDING_HANDLERS: dict[str, Callable[[Any], str]] = {
    "START": start,
    "G1": pin_snapshot,
    "G2": classify,
    "G3": build_index,
    "G4": write_context,
}
