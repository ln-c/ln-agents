"""R1 evidence, M1 (local merge branch) and RC (reconciliation) against a real git repository."""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any

import pytest
import yaml
from sandbox_env import run_on_host
from test_approvals import NAMESPACE, Signer
from test_build_gates import CLEAN, TEST_CI, Build
from test_grounding import git, repo  # noqa: F401  (repo is a fixture)

from agentic_pipeline import build_gates as bg
from agentic_pipeline import ci
from agentic_pipeline.approvals import ApprovalError, build_document
from agentic_pipeline.build_gates import BUILD_GATE_HANDLERS, latest_rebase
from agentic_pipeline.graph import load_yaml
from agentic_pipeline.policies import check_release
from agentic_pipeline.provenance import verify_provenance_chain
from agentic_pipeline.release import ReleaseError, candidate_tree, release_evidence
from agentic_pipeline.run_state import Status, load_policy
from agentic_pipeline.runner import Outcome

pytestmark = pytest.mark.skipif(
    shutil.which("ssh-keygen") is None or os.environ.get("HOME") == "/home/worker",
    reason="needs ssh-keygen and a real user",
)
ROOT = Path(__file__).resolve().parents[1]


def passing_ci() -> str:
    doc = yaml.safe_load(TEST_CI)
    ok = [["python3", "-c", "print('score 99')"]]
    doc["checks"]["mutation"].update(
        commands=ok, score={"pattern": r"score ([0-9.]+)", "minimum": 90}
    )
    return yaml.safe_dump(doc)


class Release:
    """A build whose F1 passes, with a listed approver, on a repo whose branch is `main`."""

    def __init__(
        self,
        repo: Path,  # noqa: F811
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,  # noqa: F811
    ) -> None:
        monkeypatch.setattr(ci, "run_sandboxed", run_on_host)
        git(repo, "branch", "-M", "main")
        self.signer = Signer(tmp_path)
        (repo / "policy" / "approvers.yaml").write_text(
            f"version: 1\nnamespace: {NAMESPACE}\napprovers:\n  - id: alice\n"
            f'    public_key: "{self.signer.public}"\n    decisions: [design, merge]\n'
        )
        self.repo = repo
        self.build = Build(repo, tmp_path, passing_ci())
        self.build.publish(CLEAN)
        self.runner = self.build.runner
        self.base = git(repo, "rev-parse", "HEAD")

    def f1(self) -> str:
        return BUILD_GATE_HANDLERS["F1"](self.build.ctx("F1"))

    def at_r1(self) -> None:
        self.runner.state.current = "R1"
        self.runner._save()
        assert self.runner.step().outcome is Outcome.AWAITING_HUMAN

    def sign(self, guard: str = "approve") -> tuple[bytes, str]:
        doc = build_document(
            run_id=self.runner.state.run_id,
            node="R1",
            guard=guard,
            approver="alice",
            candidate=None,
            reason="reviewed the diff",
            evidence=self.runner.approval_evidence(),
            issued_at="2026-10-15T10:00:00+00:00",
        )
        return doc, self.signer.sign(doc)

    def commit_upstream(self, files: dict[str, str]) -> str:
        for rel, text in files.items():
            (self.repo / rel).parent.mkdir(parents=True, exist_ok=True)
            (self.repo / rel).write_text(text)
        git(self.repo, "add", "--", *files)  # not the policy files the rig rewrote
        git(self.repo, "commit", "-q", "-m", "upstream")
        return git(self.repo, "rev-parse", "HEAD")


@pytest.fixture
def rel(repo: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Release:  # noqa: F811
    return Release(repo, tmp_path, monkeypatch)


def merged_branch(rel: Release) -> str:
    if not git(rel.repo, "branch", "--list", "merge/demo"):
        return ""
    return git(rel.repo, "rev-parse", "refs/heads/merge/demo")


# ── the candidate ────────────────────────────────────────────────────────────


def test_the_candidate_tree_is_the_base_with_the_patch_laid_over_it(rel: Release) -> None:
    ctx = rel.build.ctx("R1")
    _, patch, _ = bg._latest_patch(ctx)
    tree = candidate_tree(rel.repo, rel.base, patch)
    listing = git(rel.repo, "ls-tree", "-r", "--name-only", tree).split()
    assert "src/agentic_pipeline/new.py" in listing and "tests/test_x.py" in listing
    assert (
        git(rel.repo, "show", f"{tree}:src/agentic_pipeline/new.py")
        == CLEAN["src/agentic_pipeline/new.py"].strip()
    )
    assert candidate_tree(rel.repo, rel.base, patch) == tree  # deterministic
    assert git(rel.repo, "branch", "--list").split() == ["*", "main"]  # no branch was touched


def test_evidence_tracks_f1_for_this_patch_and_base(rel: Release) -> None:
    ctx = rel.build.ctx("R1")
    policy = load_policy(rel.repo)
    assert release_evidence(ctx, policy)["f1_guard"] == "not_run"
    assert rel.f1() == "all_required_pass"
    evidence = release_evidence(ctx, policy)
    assert evidence["f1_guard"] == "all_required_pass" and evidence["f1_record"].startswith(
        "sha256:"
    )
    assert evidence["target_base_sha"] == evidence["commit_sha"] == rel.base
    assert evidence["approved_sha"] == evidence["candidate_tree"]


def test_a_merge_cannot_be_approved_before_f1_passes(rel: Release) -> None:
    rel.at_r1()
    doc, sig = rel.sign("approve")
    with pytest.raises(ApprovalError, match="without passing F1"):
        rel.runner.decide(doc, sig)
    assert rel.runner.state.status is Status.AWAITING_HUMAN


# ── R1 -> M1 ─────────────────────────────────────────────────────────────────


def approve_and_merge(rel: Release) -> None:
    assert rel.f1() == "all_required_pass"
    rel.at_r1()
    doc, sig = rel.sign()
    assert rel.runner.decide(doc, sig).node == "M1"


def test_a_signed_merge_approval_creates_the_local_branch_and_leaves_main_alone(
    rel: Release,
) -> None:
    approve_and_merge(rel)
    result = rel.runner.step()
    assert (result.outcome, result.node) == (Outcome.ADVANCED, "MERGED"), result
    commit = merged_branch(rel)
    assert git(rel.repo, "rev-parse", f"{commit}^") == rel.base  # parent is the target tip
    assert git(rel.repo, "rev-parse", "main") == rel.base  # main never moved
    evidence = json.loads(
        next((rel.runner.store.run_root / "gates" / "M1").glob("*.json")).read_text()
    )
    assert evidence["merged_sha"] == commit and evidence["tree"] == git(
        rel.repo, "rev-parse", f"{commit}^{{tree}}"
    )
    log = git(rel.repo, "log", "-1", "--format=%an|%cd|%B", "--date=iso-strict", commit)
    assert "alice" in log and "agentic-pipeline|2026-10-15T10:00:00Z" in log
    approval = next(e for e in rel.runner.log.records() if e["event"] == "approval")
    assert approval["approved_sha"] == evidence["tree"] and approval["reason_code"] == "approve"
    assert verify_provenance_chain(rel.runner.log.path).ok


def test_m1_is_idempotent_and_refuses_a_branch_that_holds_something_else(rel: Release) -> None:
    approve_and_merge(rel)
    rel.runner.step()
    first = merged_branch(rel)
    rel.runner.state.current = "M1"
    rel.runner._save()
    assert rel.runner.step().node == "MERGED" and merged_branch(rel) == first
    git(rel.repo, "update-ref", "refs/heads/merge/demo", rel.base)
    rel.runner.state.current = "M1"
    rel.runner._save()
    result = rel.runner.step()
    assert result.outcome is Outcome.BLOCKED and "already exists" in result.detail


def test_m1_blocks_without_a_verified_approval(rel: Release) -> None:
    assert rel.f1() == "all_required_pass"
    rel.runner.state.current = "M1"
    rel.runner._save()
    result = rel.runner.step()
    assert result.outcome is Outcome.BLOCKED and "no verified R1 merge approval" in result.detail
    assert git(rel.repo, "branch", "--list").split() == ["*", "main"]


def test_a_stored_approval_with_a_bad_signature_does_not_count(rel: Release) -> None:
    approve_and_merge(rel)
    (sig,) = (rel.runner.store.run_root / "approvals").glob("*.sig")
    sig.write_text("-----BEGIN SSH SIGNATURE-----\nforged\n-----END SSH SIGNATURE-----\n")
    result = rel.runner.step()
    assert result.outcome is Outcome.BLOCKED and "no verified R1 merge approval" in result.detail


def test_deny_and_cancel_are_signed_too(rel: Release) -> None:
    rel.at_r1()
    doc, sig = rel.sign("cancel")
    assert rel.runner.decide(doc, sig).node == "T-CANCELED"


# ── a moved target branch: M1 -> RC ──────────────────────────────────────────


def moved(rel: Release, files: dict[str, str]) -> str:
    approve_and_merge(rel)
    tip = rel.commit_upstream(files)
    result = rel.runner.step()
    assert (result.outcome, result.node) == (Outcome.ADVANCED, "RC"), result
    return tip


def test_a_moved_target_branch_sends_m1_to_rc_without_merging(rel: Release) -> None:
    moved(rel, {"src/other.py": "x = 1\n"})
    assert not merged_branch(rel)


def test_rc_rebases_onto_unrelated_upstream_work_and_reruns_e2_and_f1(rel: Release) -> None:
    tip = moved(rel, {"src/other.py": "x = 1\n"})
    old_doc = rel.runner.store.run_root / "approvals"
    result = rel.runner.step()  # RC -> E2 (reconcile budget)
    assert (result.outcome, result.node) == (Outcome.ADVANCED, "E2"), result
    assert latest_rebase(rel.runner.store.run_root)["new_base"] == tip
    assert bg.tested_base(rel.build.ctx("E2")) == tip
    final = rel.runner.run(10)  # E2 -> F1 -> R1
    assert (final.outcome, final.node) == (Outcome.AWAITING_HUMAN, "R1"), final
    assert rel.runner.state.ledger["consumed"].get("reconcile") == 1
    f1 = json.loads(
        sorted((rel.runner.store.run_root / "gates" / "F1").glob("*.json"))[-1].read_text()
    )
    assert f1["base"] == tip
    stale = next(old_doc.glob("*.json"))
    with pytest.raises(ApprovalError, match="evidence changed"):
        rel.runner.decide(stale.read_bytes(), stale.with_name(stale.name + ".sig").read_text())
    doc, sig = rel.sign()  # a new approval for the rebased candidate
    assert rel.runner.decide(doc, sig).node == "M1"
    assert rel.runner.step().node == "MERGED"
    commit = merged_branch(rel)
    assert git(rel.repo, "rev-parse", f"{commit}^") == tip
    assert git(rel.repo, "show", f"{commit}:src/other.py") == "x = 1"  # upstream work kept
    assert git(rel.repo, "show", f"{commit}:src/agentic_pipeline/new.py")  # and the patch


@pytest.mark.parametrize(
    "files",
    [{"tests/test_new.py": "x\n"}, {"docs/adr/0002-new.md": "# n\n"}, {"contracts/a.md": "c\n"}],
)
def test_a_change_to_a_constraint_sends_the_run_to_child(
    rel: Release, files: dict[str, str]
) -> None:
    moved(rel, files)
    result = rel.runner.step()
    assert (result.outcome, result.node) == (Outcome.ADVANCED, "CHILD"), result
    assert latest_rebase(rel.runner.store.run_root) is None


def test_rc_blocks_when_the_patch_and_upstream_touch_the_same_file(rel: Release) -> None:
    rel.build.publish({**CLEAN, "src/shared.py": "mine = 1\n"})
    assert rel.f1() == "all_required_pass"
    rel.at_r1()
    doc, sig = rel.sign()
    rel.runner.decide(doc, sig)
    rel.commit_upstream({"src/shared.py": "theirs = 1\n"})
    assert rel.runner.step().node == "RC"
    result = rel.runner.step()
    assert result.outcome is Outcome.BLOCKED and "src/shared.py" in result.detail


def test_rc_refuses_when_there_is_nothing_to_reconcile(rel: Release) -> None:
    rel.runner.state.current = "RC"
    rel.runner._save()
    result = rel.runner.step()
    assert result.outcome is Outcome.BLOCKED and "nothing to reconcile" in result.detail


# ── policy/release.yaml ──────────────────────────────────────────────────────


def test_the_shipped_release_policy_is_valid() -> None:
    assert check_release(load_yaml(ROOT / "policy" / "release.yaml")) == []


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d.update(target_branch="a b"),
        lambda d: d.update(target_branch="../main"),
        lambda d: d.update(merge_branch_prefix="merge"),
        lambda d: d.update(merge_branch_prefix="main/"),
        lambda d: d.update(constraint_paths=[]),
        lambda d: d.update(constraint_paths=["../x"]),
        lambda d: d.update(constraint_paths=["/abs"]),
    ],
)
def test_malformed_release_policy_is_rejected(mutate: Any) -> None:
    doc = load_yaml(ROOT / "policy" / "release.yaml")
    mutate(doc)
    assert check_release(doc)


def test_unknown_target_branch_is_an_error(rel: Release) -> None:
    git(rel.repo, "branch", "-m", "main", "trunk")
    with pytest.raises(ReleaseError, match="does not exist"):
        release_evidence(rel.build.ctx("R1"), load_policy(rel.repo))


def test_cli_approve_shows_the_merge_evidence(
    rel: Release, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    import argparse

    from agentic_pipeline import run_cli

    assert rel.f1() == "all_required_pass"
    rel.at_r1()
    rel.runner.close()
    out = tmp_path / "r1.json"
    ns = argparse.Namespace(
        root=rel.repo,
        runs_dir=rel.build.rig.run_dir.parent,
        run_id="demo",
        decision="approve",
        approver="alice",
        reason="reviewed",
        candidate=None,
        out=out,
        run_command="approve",
    )
    assert run_cli.run_command(ns) == 0
    shown = capsys.readouterr().out
    assert "F1 for this patch and base: all_required_pass" in shown and "candidate tree" in shown
    assert json.loads(out.read_text())["evidence"]["target_branch"] == "main"
