"""Release-side gates: R1 evidence, M1 (merge to a local branch) and RC (reconciliation).

The candidate is the pinned base commit plus the implementation patch. `candidate_tree` builds
that tree with git plumbing in a throwaway index, so no working tree or branch is touched, and
its git tree id is what a merge approval binds to (as `approved_sha`).

R1: a signed `merge` approval (see approvals.py) is bound to the tested base, the target branch
tip, the candidate tree, the patch id and the newest F1 result for that patch and base.

MERGED waits for the human merge of the local branch into the target branch and records the commit
that carries the approved patch (a fast-forward or merge commit that contains the branch commit, or
a squash or rebase commit whose own change is exactly the patch, even after the target moved). It
runs L2 (a learning record) as a side effect, or ends the run when the merge branch was deleted
unmerged. RM routes a denied merge by the cause the human signed into the denial. R2 evidence is
the merged SHA; no deployment environment is configurable yet, so R2 can only deny or decline.

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
    REMEDIATION_CAUSES,
    ApprovalError,
    parse_signed,
)
from agentic_pipeline.build_gates import (
    REBASED,
    BuildGateError,
    _latest_patch,
    tested_base,
)
from agentic_pipeline.gates import GateError, _policy, _record, _sealed

if TYPE_CHECKING:
    from agentic_pipeline.run_state import PolicySnapshot
    from agentic_pipeline.runner import HandlerContext

GIT_TIMEOUT_SECONDS = 60
IDENTITY = {"name": "agentic-pipeline", "email": "agentic-pipeline@localhost"}
NO_COMMIT = "0" * 40


class ReleaseError(GateError):
    pass


def merge_branch(prefix: str, run_id: str, tree: str) -> str:
    """Branch for one approved candidate: a remediated candidate gets its own branch."""
    return f"{prefix}{run_id}-{tree[:12]}"


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
    ref = "refs/heads/" + merge_branch(
        rp["merge_branch_prefix"], ctx.run_id, evidence["candidate_tree"]
    )
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
            "commit": commit,
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


def _newest(folder: Path, accept: Callable[[dict[str, Any]], bool]) -> dict[str, Any] | None:
    for path in sorted(folder.glob("*.json"), key=lambda p: p.stat().st_mtime_ns, reverse=True):
        doc = json.loads(path.read_text("utf-8"))
        if accept(doc):
            return doc
    return None


def _patch_files(patch: Path) -> dict[str, Path]:
    return {p.relative_to(patch).as_posix(): p for p in sorted(patch.rglob("*")) if p.is_file()}


def _carries_the_patch(root: Path, commit: str, files: Mapping[str, Path]) -> bool:
    """True if `commit` changes exactly the patch's files, to exactly the patch's content."""
    for name, path in files.items():
        want = _git(root, "hash-object", "--", str(path)).stdout.strip()
        have = _git(root, "rev-parse", "--verify", "--quiet", f"{commit}:{name}", check=False)
        if have.returncode != 0 or have.stdout.strip() != want:
            return False
    touched = set(
        _git(root, "diff-tree", "--no-commit-id", "--name-only", "-r", commit).stdout.split()
    )
    return touched <= set(files)


def find_merge(
    root: Path, base: str, tip: str, merge_commit: str | None, files: Mapping[str, Path]
) -> tuple[str, str] | None:
    """(commit on the target branch, how) that carries the approved patch, or None.

    Oldest first along the target's first-parent history after the tested base: a commit that
    contains the branch commit (fast-forward or merge commit), or one whose own change is exactly
    the patch (a squash or rebase merge, even after the target moved).
    """
    history = _git(root, "rev-list", "--first-parent", "--reverse", f"{base}..{tip}").stdout.split()
    for commit in history:
        if (
            merge_commit
            and _git(
                root, "merge-base", "--is-ancestor", merge_commit, commit, check=False
            ).returncode
            == 0
        ):
            return commit, "contains_merge_branch"
        if _carries_the_patch(root, commit, files):
            return commit, "equivalent_change"
    return None


def latest_merged(run_root: Path) -> dict[str, Any] | None:
    return _newest(run_root / "gates" / "MERGED", lambda d: d.get("guard") == "merged")


def merged(ctx: HandlerContext) -> str:
    """MERGED: wait for the human merge, record the merged SHA, or end an abandoned run."""
    rp = _policy(ctx).release
    evidence = release_evidence(ctx, _policy(ctx))
    _approved(ctx, evidence)  # the candidate must still be the one a human approved
    stage, patch, _ = _latest_patch(ctx)
    branch = merge_branch(rp["merge_branch_prefix"], ctx.run_id, evidence["candidate_tree"])
    made = _newest(
        ctx.run_root / "gates" / "M1",
        lambda d: d.get("tree") == evidence["candidate_tree"] and d.get("guard") == "success",
    )
    if made is None:
        raise ReleaseError("M1 has not created a merge branch for this candidate")
    tip = evidence["target_base_sha"]
    found = find_merge(
        ctx.root, evidence["commit_sha"], tip, made["merged_sha"], _patch_files(patch)
    )
    if found is None:
        still_there = _git(
            ctx.root, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}", check=False
        )
        if still_there.returncode != 0:
            _record(
                ctx,
                "MERGED",
                f"abandoned-{made['merged_sha']}",
                {"guard": "abandoned", "branch": branch},
            )
            return "abandoned"
        raise ReleaseError(
            f"waiting for {branch} (or an equivalent change) to reach {rp['target_branch']}"
        )
    commit, how = found
    _record(
        ctx,
        "MERGED",
        commit,
        {
            "guard": "merged",
            "merged_sha": commit,
            "how": how,
            "branch": branch,
            "branch_commit": made["merged_sha"],
            "tree": evidence["candidate_tree"],
            "target_branch": rp["target_branch"],
            "tested_base": evidence["commit_sha"],
            "patch": evidence["patch"],
        },
    )
    return "merged"


def learn(ctx: HandlerContext) -> str:
    """L2 (a side effect of MERGED): the merged-only learning record. It edits nothing in the repo.

    It lists the ADRs the pinned manifest holds as accepted; marking them implemented is a human
    change to docs/adr. The record's digest goes into the run's hash-chained provenance log as
    a `learning` event, once per merged SHA, so a retried step never appends it twice.
    """
    done = latest_merged(ctx.run_root)
    if done is None:
        raise ReleaseError("L2 runs only after a merge has been recorded")
    manifest = _sealed(ctx, ["pinned_context_manifest"])["pinned_context_manifest"]
    adrs = json.loads((manifest / "context_manifest.json").read_text("utf-8"))["adrs"]
    path = _record(
        ctx,
        "L2",
        done["merged_sha"],
        {
            "merged_sha": done["merged_sha"],
            "run_id": ctx.run_id,
            "patch": done["patch"],
            "accepted_adrs": sorted(a["id"] for a in adrs if a["status"] == "accepted"),
            "marked_implemented": False,
        },
    )
    digest = "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
    if not any(
        e["event"] == "learning" and e["merged_sha"] == done["merged_sha"] for e in ctx.events()
    ):
        ctx.append(
            {
                "state": "L2",
                "event": "learning",
                "merged_sha": done["merged_sha"],
                "record_digest": digest,
            }
        )
    return "always"


def _signed_decisions(ctx: HandlerContext, node: str) -> list[dict[str, Any]]:
    """Stored approval documents for this run and node that verify, oldest first."""
    approvers = _policy(ctx).approvers
    found = []
    for path in (ctx.run_root / "approvals").glob("*.json"):
        try:
            doc = parse_signed(
                path.read_bytes(),
                path.with_name(path.name + ".sig").read_text(encoding="utf-8"),
                approvers=approvers,
                node=node,
            )
        except (ApprovalError, OSError):
            continue
        if doc["run_id"] == ctx.run_id and doc["node"] == node:
            found.append(doc)
    return sorted(found, key=lambda d: d["issued_at"])


def remediate(ctx: HandlerContext) -> str:
    """RM: route by the cause the human signed into the newest R1 denial."""
    denials = [d for d in _signed_decisions(ctx, "R1") if d["decision"] == "deny"]
    if not denials:
        raise ReleaseError("RM needs a signed R1 denial")
    cause = denials[-1]["remediation_cause"]
    if cause not in REMEDIATION_CAUSES:
        raise ReleaseError(f"the newest R1 denial names no valid cause: {cause!r}")
    _record(
        ctx,
        "RM",
        f"{cause}-{denials[-1]['issued_at']}",
        {"guard": cause, "reason": denials[-1]["reason"]},
    )
    return cause


def deploy_evidence(ctx: HandlerContext, policy: PolicySnapshot) -> dict[str, Any]:
    """What an approver at R2 is shown. No deployment environment is configurable yet."""
    done = latest_merged(ctx.run_root)
    if done is None:
        raise ReleaseError("R2 needs a recorded merge")
    approvals = [d for d in _signed_decisions(ctx, "R1") if d["decision"] == MERGE_APPROVE_GUARD]
    return {
        "commit_sha": done["tested_base"],
        "approved_sha": done["merged_sha"],
        "merged_sha": done["merged_sha"],
        "target_branch": done["target_branch"],
        "patch": done["patch"],
        "environments": [],
        "merge_approver": approvals[-1]["approver"] if approvals else None,
        "separate_deploy_approver": bool(policy.release.get("separate_deploy_approver", False)),
    }


RELEASE_HANDLERS: dict[str, Callable[[Any], str]] = {
    "M1": merge,
    "RC": reconcile,
    "MERGED": merged,
    "L2": learn,
    "RM": remediate,
}
