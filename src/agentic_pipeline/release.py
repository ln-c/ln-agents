"""Release-side gates: R1 evidence, M1 (merge to a local branch) and RC (reconciliation).

The candidate is the pinned base commit plus the implementation patch. `candidate_tree` builds
that tree with git plumbing in a throwaway index, so no working tree or branch is touched, and
its git tree id is what a merge approval binds to (as `approved_sha`).

R1: a signed `merge` approval (see approvals.py) is bound to the tested base, the target branch
tip, the candidate tree, the patch id and the newest F1 result for that patch and base.

M1 merges only if a signed R1 approval for exactly this candidate exists in the run, F1 passed,
and the target branch tip is still the tested base. It never writes to the target branch: it
creates `<merge_branch_prefix><run id>` with the target tip as parent and the candidate tree,
using the approval's timestamp so a rerun yields the same commit. If the target branch moved
it routes to RC instead. A human pushes the branch or opens the pull request.

RC compares the tested base with the new tip. A change under `constraint_paths` means the run's
grounding is stale (CHILD). Otherwise, if the patch and the upstream change touch different files,
RC records the new tip as the tested base, so E2 and F1 run again on it and a new approval is
needed (the evidence changes). Anything it cannot establish blocks, because the graph offers no
guard for it. Git writes are limited to objects and the one merge branch.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from collections.abc import Callable, Mapping
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any

from agentic_pipeline.approvals import (
    MERGE_APPROVE_GUARD,
    ApprovalError,
    parse_signed,
)
from agentic_pipeline.build_gates import (
    REBASED,
    BuildGateError,
    _latest_patch,
    tested_base,
)
from agentic_pipeline.gates import GateError, _policy, _record

if TYPE_CHECKING:
    from agentic_pipeline.run_state import PolicySnapshot
    from agentic_pipeline.runner import HandlerContext

GIT_TIMEOUT_SECONDS = 60
IDENTITY = {"name": "agentic-pipeline", "email": "agentic-pipeline@localhost"}
NO_COMMIT = "0" * 40


class ReleaseError(GateError):
    pass


def _git(
    root: Path, *args: str, env: Mapping[str, str] | None = None, check: bool = True
) -> subprocess.CompletedProcess[str]:
    base = {
        "PATH": os.environ.get("PATH", "/usr/bin"),
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "LC_ALL": "C",
    }
    git = shutil.which("git")
    if git is None:
        raise ReleaseError("git is not installed")
    try:
        done = subprocess.run(  # noqa: S603 - fixed git subcommands, no shell
            [git, "-C", str(root), *args],
            env={**base, **(env or {})},
            capture_output=True,
            text=True,
            timeout=GIT_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise ReleaseError(f"git {args[0]} failed: {exc}") from exc
    if check and done.returncode != 0:
        raise ReleaseError(f"git {args[0]} failed: {done.stderr.strip()}")
    return done


def candidate_tree(root: Path, base: str, patch: Path) -> str:
    """Git tree id of `base` with the patch's files laid over it."""
    with tempfile.TemporaryDirectory(prefix="candidate-") as tmp:
        env = {"GIT_INDEX_FILE": str(Path(tmp) / "index")}
        _git(root, "read-tree", base, env=env)
        for file in sorted(p for p in patch.rglob("*") if p.is_file()):
            rel = PurePosixPath(file.relative_to(patch).as_posix())
            if rel.is_absolute() or ".." in rel.parts or file.is_symlink():
                raise ReleaseError(f"{rel}: not a plain path inside the repository")
            blob = _git(root, "hash-object", "-w", "--", str(file)).stdout.strip()
            mode = "100755" if os.access(file, os.X_OK) else "100644"
            _git(root, "update-index", "--add", "--cacheinfo", f"{mode},{blob},{rel}", env=env)
        return _git(root, "write-tree", env=env).stdout.strip()


def target_tip(root: Path, branch: str) -> str:
    done = _git(root, "rev-parse", "--verify", f"refs/heads/{branch}^{{commit}}", check=False)
    if done.returncode != 0:
        raise ReleaseError(f"target branch {branch!r} does not exist in the repository")
    return done.stdout.strip()


def latest_f1(ctx: HandlerContext, patch_id: str, base: str) -> tuple[str, str | None]:
    """(guard, record digest) of the newest F1 record for this patch on this base."""
    records = sorted(
        (ctx.run_root / "gates" / "F1").glob("*.json"), key=lambda p: p.stat().st_mtime_ns
    )
    for path in reversed(records):
        doc = json.loads(path.read_text("utf-8"))
        if doc.get("patch") == patch_id and doc.get("base") == base:
            return str(doc["guard"]), "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
    return "not_run", None


def release_evidence(ctx: HandlerContext, policy: PolicySnapshot) -> dict[str, Any]:
    """What an approver at R1 is shown, recomputed from the patch, git and the F1 records."""
    rp = policy.release
    base = tested_base(ctx)
    stage, patch, digest = _latest_patch(ctx)
    patch_id = f"{stage}:{digest}"
    tree = candidate_tree(ctx.root, base, patch)
    guard, record = latest_f1(ctx, patch_id, base)
    return {
        "commit_sha": base,
        "approved_sha": tree,
        "target_branch": rp["target_branch"],
        "target_base_sha": target_tip(ctx.root, rp["target_branch"]),
        "candidate_tree": tree,
        "patch": patch_id,
        "f1_guard": guard,
        "f1_record": record,
    }


def _approved(ctx: HandlerContext, evidence: Mapping[str, Any]) -> tuple[dict[str, Any], str]:
    """The stored, signed R1 approval for exactly this candidate, verified again."""
    approvers = _policy(ctx).approvers
    folder = ctx.run_root / "approvals"
    for path in sorted(folder.glob("*.json")):
        document = path.read_bytes()
        try:
            doc = parse_signed(
                document,
                path.with_name(path.name + ".sig").read_text(encoding="utf-8"),
                approvers=approvers,
                node="R1",
            )
        except (ApprovalError, OSError):
            continue
        said = doc["evidence"]
        if (
            doc["run_id"] == ctx.run_id
            and doc["node"] == "R1"
            and doc["decision"] == MERGE_APPROVE_GUARD
            and said.get("candidate_tree") == evidence["candidate_tree"]
            and said.get("commit_sha") == evidence["commit_sha"]
            and said.get("patch") == evidence["patch"]
            and said.get("f1_guard") == "all_required_pass"
        ):
            return doc, "sha256:" + hashlib.sha256(document).hexdigest()
    raise ReleaseError("no verified R1 merge approval for this exact candidate")


def merge(ctx: HandlerContext) -> str:
    """M1: create the local merge branch for an approved candidate, or send a moved base to RC."""
    rp = _policy(ctx).release
    evidence = release_evidence(ctx, _policy(ctx))
    doc, approval_digest = _approved(ctx, evidence)
    if evidence["f1_guard"] != "all_required_pass":
        raise ReleaseError("F1 has not passed for this patch on this base")
    tip = evidence["target_base_sha"]
    if tip != evidence["commit_sha"] or tip != doc["evidence"]["target_base_sha"]:
        _record(
            ctx,
            "M1",
            f"moved-{tip}",
            {
                "guard": "head_changed_or_conflict",
                "tested_base": evidence["commit_sha"],
                "tip": tip,
            },
        )
        return "head_changed_or_conflict"
    when = doc["issued_at"]
    env = {
        "GIT_AUTHOR_NAME": IDENTITY["name"],
        "GIT_AUTHOR_EMAIL": IDENTITY["email"],
        "GIT_AUTHOR_DATE": when,
        "GIT_COMMITTER_NAME": IDENTITY["name"],
        "GIT_COMMITTER_EMAIL": IDENTITY["email"],
        "GIT_COMMITTER_DATE": when,
    }
    message = (
        f"pipeline: run {ctx.run_id}\n\nApproved-by: {doc['approver']}\n"
        f"Approval: {approval_digest}\nPatch: {evidence['patch']}\nTested-base: {tip}\n"
    )
    commit = _git(
        ctx.root, "commit-tree", evidence["candidate_tree"], "-p", tip, "-m", message, env=env
    ).stdout.strip()
    ref = f"refs/heads/{rp['merge_branch_prefix']}{ctx.run_id}"
    existing = _git(ctx.root, "rev-parse", "--verify", "--quiet", ref, check=False)
    if existing.returncode == 0:
        if existing.stdout.strip() != commit:
            raise ReleaseError(f"{ref} already exists with a different commit")
    else:
        _git(ctx.root, "update-ref", ref, commit, NO_COMMIT)
    _record(
        ctx,
        "M1",
        commit,
        {
            "guard": "success",
            "merged_sha": commit,
            "branch": ref,
            "parent": tip,
            "tree": evidence["candidate_tree"],
            "approval": approval_digest,
            "patch": evidence["patch"],
        },
    )
    return "success"


def reconcile(ctx: HandlerContext) -> str:
    """RC: classify what changed upstream since the tested base."""
    rp = _policy(ctx).release
    old = tested_base(ctx)
    tip = target_tip(ctx.root, rp["target_branch"])
    if tip == old:
        raise ReleaseError("the target branch has not moved; there is nothing to reconcile")
    if _git(ctx.root, "merge-base", "--is-ancestor", old, tip, check=False).returncode != 0:
        raise ReleaseError(f"{tip} does not descend from the tested base {old}; cannot reconcile")
    paths = list(rp["constraint_paths"])
    changed = _git(ctx.root, "diff", "--name-only", old, tip, "--", *paths).stdout.split()
    if changed:
        _record(
            ctx,
            "RC",
            f"changed-{tip}",
            {"guard": "constraints_changed", "old_base": old, "tip": tip, "changed": changed},
        )
        return "constraints_changed"
    try:
        _, patch, _ = _latest_patch(ctx)
    except BuildGateError as exc:
        raise ReleaseError(str(exc)) from exc
    mine = {p.relative_to(patch).as_posix() for p in patch.rglob("*") if p.is_file()}
    theirs = set(_git(ctx.root, "diff", "--name-only", old, tip).stdout.split())
    if overlap := sorted(mine & theirs):
        raise ReleaseError(
            f"the patch and the upstream change both touch {overlap}; a human must reconcile"
        )
    _record(
        ctx,
        "RC",
        f"rebased-{tip}",
        {"guard": REBASED, "old_base": old, "new_base": tip, "upstream_files": sorted(theirs)},
    )
    return REBASED


RELEASE_HANDLERS: dict[str, Callable[[Any], str]] = {"M1": merge, "RC": reconcile}
