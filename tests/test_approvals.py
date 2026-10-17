"""Signed human approvals: verification, authority, freshness, and the runner's H1 decision."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest
from test_gates import Design, blocking, critique
from test_grounding import repo  # noqa: F401  (a fixture)
from test_runner import Rig, policy_copy, stored

from agentic_pipeline import approvals
from agentic_pipeline.approvals import (
    APPROVE_GUARD,
    ApprovalError,
    build_document,
    canonical,
    verify_approval,
)
from agentic_pipeline.policies import check_approvers
from agentic_pipeline.provenance import verify_provenance_chain
from agentic_pipeline.run_state import Status
from agentic_pipeline.runner import Outcome
from agentic_pipeline.sandbox import HOME

IN_CI_SANDBOX = os.environ.get("HOME") == HOME  # ssh-keygen needs a passwd entry there
pytestmark = pytest.mark.skipif(
    shutil.which("ssh-keygen") is None or IN_CI_SANDBOX, reason="needs ssh-keygen and a real user"
)

KEYGEN = shutil.which("ssh-keygen") or "ssh-keygen"
NAMESPACE = "agentic-pipeline-approval"
GUARDS = ["approve_design_and_no_security_veto", "reject", "halt", "revise_with_notes"]
SHA = "a" * 40
EVIDENCE: dict[str, Any] = {
    "commit_sha": SHA,
    "context_manifest": "sha256:" + "1" * 64,
    "design_inputs": "sha256:" + "2" * 64,
    "vetoes": "sha256:" + "3" * 64,
    "scores": "sha256:" + "4" * 64,
    "survivors": ["proposal_b_sealed"],
    "fired": {"proposal_a_sealed": ["data_class_violation"], "proposal_b_sealed": []},
    "undetermined": {"proposal_a_sealed": [], "proposal_b_sealed": ["known_vulnerable_dependency"]},
    "unevaluated": [],
    "provenance_tip": "sha256:" + "5" * 64,
    "policy": {"pipeline/graph.yaml": "sha256:" + "6" * 64},
}


class Signer:
    def __init__(self, directory: Path, name: str = "key") -> None:
        self.key = directory / name
        subprocess.run(  # noqa: S603
            [KEYGEN, "-q", "-t", "ed25519", "-N", "", "-C", "", "-f", str(self.key)],
            check=True,
        )
        self.public = " ".join(self.key.with_suffix(".pub").read_text().split()[:2])

    def sign(self, document: bytes, namespace: str = NAMESPACE) -> str:
        message = self.key.parent / "message.json"
        message.write_bytes(document)
        message.with_name("message.json.sig").unlink(missing_ok=True)
        subprocess.run(  # noqa: S603
            [KEYGEN, "-q", "-Y", "sign", "-f", str(self.key), "-n", namespace, str(message)],
            check=True,
        )
        return message.with_name("message.json.sig").read_text()


def policy_for(signer: Signer, decisions: list[str] | None = None) -> dict[str, Any]:
    return {
        "version": 1,
        "namespace": NAMESPACE,
        "approvers": [
            {
                "id": "alice",
                "public_key": signer.public,
                "decisions": decisions or list(approvals.DECISION_TYPES),
            }
        ],
    }


@pytest.fixture
def signer(tmp_path: Path) -> Signer:
    return Signer(tmp_path)


def document(**over: Any) -> bytes:
    args: dict[str, Any] = {
        "run_id": "run-1",
        "node": "H1",
        "guard": APPROVE_GUARD,
        "approver": "alice",
        "candidate": "proposal_b_sealed",
        "reason": "the design is sound",
        "evidence": EVIDENCE,
        "issued_at": "2026-10-14T00:00:00+00:00",
    }
    return build_document(**{**args, **over})


def verify(doc: bytes, sig: str, signer: Signer, **over: Any):
    args: dict[str, Any] = {
        "approvers": policy_for(signer),
        "run_id": "run-1",
        "node": "H1",
        "guards": GUARDS,
        "evidence": EVIDENCE,
    }
    return verify_approval(doc, sig, **{**args, **over})


def test_a_correctly_signed_approval_verifies(signer: Signer) -> None:
    doc = document()
    result = verify(doc, signer.sign(doc), signer)
    assert (result.approver, result.guard, result.candidate) == (
        "alice",
        APPROVE_GUARD,
        "proposal_b_sealed",
    )
    assert result.approved_sha == SHA and result.digest.startswith("sha256:")


def test_a_signature_from_another_key_is_refused(signer: Signer, tmp_path: Path) -> None:
    doc = document()
    other = Signer(tmp_path / "x" if (tmp_path / "x").mkdir() is None else tmp_path, "other")
    with pytest.raises(ApprovalError, match="does not verify"):
        verify(doc, other.sign(doc), signer)


def test_a_signature_in_another_namespace_is_refused(signer: Signer) -> None:
    doc = document()
    with pytest.raises(ApprovalError, match="does not verify"):
        verify(doc, signer.sign(doc, namespace="git"), signer)


def test_a_document_changed_after_signing_is_refused(signer: Signer) -> None:
    doc = document()
    sig = signer.sign(doc)
    forged = canonical({**json.loads(doc), "reason": "something else"})
    with pytest.raises(ApprovalError, match="does not verify"):
        verify(forged, sig, signer)


def test_a_non_canonical_document_is_refused(signer: Signer) -> None:
    doc = json.dumps(json.loads(document()), indent=2).encode()
    with pytest.raises(ApprovalError, match="canonical"):
        verify(doc, signer.sign(doc), signer)


def test_an_unlisted_approver_is_refused(signer: Signer) -> None:
    doc = document(approver="mallory")
    with pytest.raises(ApprovalError, match="not listed"):
        verify(doc, signer.sign(doc), signer)


def test_an_approver_without_the_design_authority_is_refused(signer: Signer) -> None:
    doc = document()
    with pytest.raises(ApprovalError, match="may not make"):
        verify(doc, signer.sign(doc), signer, approvers=policy_for(signer, ["merge"]))


@pytest.mark.parametrize(
    ("over", "match"),
    [
        ({"run_id": "run-2"}, "not run-1/H1"),
        ({"guards": ["reject"]}, "not a decision available"),
        ({"evidence": {**EVIDENCE, "provenance_tip": "sha256:" + "9" * 64}}, "evidence changed"),
        ({"evidence": {**EVIDENCE, "design_inputs": "sha256:" + "8" * 64}}, "evidence changed"),
    ],
)
def test_stale_or_misdirected_approvals_are_refused(
    signer: Signer, over: dict[str, Any], match: str
) -> None:
    doc = document(**({"run_id": "run-2"} if "run_id" in over else {}))
    kwargs = {k: v for k, v in over.items() if k != "run_id"}
    with pytest.raises(ApprovalError, match=match):
        verify(doc, signer.sign(doc), signer, **kwargs)


def test_a_candidate_with_a_fired_veto_can_never_be_approved(signer: Signer) -> None:
    doc = document(candidate="proposal_a_sealed")
    with pytest.raises(ApprovalError, match="cannot be approved"):
        verify(doc, signer.sign(doc), signer)


def test_other_decisions_name_no_candidate_and_need_a_reason(signer: Signer) -> None:
    doc = document(guard="reject", candidate="proposal_b_sealed")
    with pytest.raises(ApprovalError, match="only a design approval names a candidate"):
        verify(doc, signer.sign(doc), signer)
    doc = document(guard="reject", candidate=None, reason="  ")
    with pytest.raises(ApprovalError, match="needs a reason"):
        verify(doc, signer.sign(doc), signer)
    doc = document(guard="reject", candidate=None)
    assert verify(doc, signer.sign(doc), signer).guard == "reject"


def test_only_h1_r1_and_r2_are_wired() -> None:
    with pytest.raises(ApprovalError, match="not wired"):
        document(node="HR")


def test_the_repo_approvers_policy_is_valid() -> None:
    from agentic_pipeline.graph import load_yaml

    root = Path(__file__).resolve().parents[1]
    assert check_approvers(load_yaml(root / "policy" / "approvers.yaml")) == []


@pytest.mark.parametrize(
    "mutate",
    [
        lambda d: d.update(approvers=[]),
        lambda d: d.update(namespace="has space"),
        lambda d: d["approvers"].append(dict(d["approvers"][0])),
        lambda d: d["approvers"][0].update(public_key="ssh-rsa AAAA"),
        lambda d: d["approvers"][0].update(public_key="ssh-ed25519 AAAA comment"),
        lambda d: d["approvers"][0].update(decisions=["everything"]),
        lambda d: d["approvers"][0].update(decisions=[]),
    ],
)
def test_malformed_approvers_policy_is_rejected(signer: Signer, mutate: Any) -> None:
    doc = policy_for(signer)
    assert check_approvers(doc) == []
    mutate(doc)
    assert check_approvers(doc)


# ── the runner at H1 ─────────────────────────────────────────────────────────


def list_signer(root: Path, signer: Signer) -> None:
    (root / "policy" / "approvers.yaml").write_text(
        "version: 1\n"
        f"namespace: {NAMESPACE}\n"
        "approvers:\n"
        f'  - id: alice\n    public_key: "{signer.public}"\n'
        "    decisions: [design]\n"
    )


def h1(tmp_path: Path, signer: Signer, monkeypatch: pytest.MonkeyPatch):
    """A runner awaiting a human at H1 under a repo copy that lists the test signer."""
    root = policy_copy(tmp_path)
    list_signer(root, signer)
    rig = Rig(tmp_path, root)
    runner = rig.at("H1")
    assert runner.step().outcome is Outcome.AWAITING_HUMAN
    base = {k: v for k, v in EVIDENCE.items() if k not in ("provenance_tip", "policy")}
    monkeypatch.setattr("agentic_pipeline.runner.approval_evidence", lambda ctx, policy: base)
    return rig, runner


def signed(runner: Any, signer: Signer, guard: str, candidate: str | None) -> tuple[bytes, str]:
    doc = build_document(
        run_id=runner.state.run_id,
        node="H1",
        guard=guard,
        approver="alice",
        candidate=candidate,
        reason="reviewed",
        evidence=runner.approval_evidence(),
        issued_at="2026-10-14T00:00:00+00:00",
    )
    return doc, signer.sign(doc)


def test_a_signed_design_approval_moves_h1_to_dr_and_is_logged(
    tmp_path: Path, signer: Signer, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig, runner = h1(tmp_path, signer, monkeypatch)
    doc, sig = signed(runner, signer, APPROVE_GUARD, "proposal_b_sealed")
    result = runner.decide(doc, sig)
    assert (result.outcome, result.node) == (Outcome.ADVANCED, "DR")
    assert stored(rig).current == "DR" and stored(rig).status is Status.RUNNING
    events = rig.events()
    approval = next(e for e in events if e["event"] == "approval")
    assert approval["approver_id"] == "alice" and approval["approved_sha"] == SHA
    assert approval["approval_digest"] == approvals.digest(doc)
    assert verify_provenance_chain(rig.log.path).ok


def test_an_approval_cannot_be_used_twice(
    tmp_path: Path, signer: Signer, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig, runner = h1(tmp_path, signer, monkeypatch)
    doc, sig = signed(runner, signer, "reject", None)
    assert runner.decide(doc, sig).node == "T-REJECTED"
    # The run is no longer at H1, and the log tip has moved: the same file is refused.
    with pytest.raises(ApprovalError, match="needs awaiting_human"):
        runner.decide(doc, sig)


def test_a_refused_approval_changes_nothing(
    tmp_path: Path, signer: Signer, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig, runner = h1(tmp_path, signer, monkeypatch)
    doc, _ = signed(runner, signer, "reject", None)
    before = ((rig.run_dir / "state.json").read_text(), rig.log.path.read_text())
    with pytest.raises(ApprovalError, match="does not verify"):
        runner.decide(doc, "-----BEGIN SSH SIGNATURE-----\nbogus\n-----END SSH SIGNATURE-----\n")
    assert ((rig.run_dir / "state.json").read_text(), rig.log.path.read_text()) == before
    assert runner.state.status is Status.AWAITING_HUMAN


def test_an_approval_issued_before_the_log_moved_is_stale(
    tmp_path: Path, signer: Signer, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig, runner = h1(tmp_path, signer, monkeypatch)
    doc, sig = signed(runner, signer, "reject", None)
    runner.log.append({"state": "H1", "event": "decision", "reason_code": "note"})
    with pytest.raises(ApprovalError, match="evidence changed"):
        runner.decide(doc, sig)
    assert stored(rig).current == "H1"


def h1_before_synthesis(repo: Path, tmp_path: Path, signer: Signer) -> Design:  # noqa: F811
    """A real grounded run with A1 sealed and a blocking critique, as A2 leaves it at H1.

    `design_spent` sends A2 to H1 before A3 runs (test_runner covers that edge), so no synthesis
    exists. This rig has no model dispatcher, so the run is placed at H1 rather than walked there.
    """
    list_signer(repo, signer)
    design = Design(repo, tmp_path)
    design.seal_proposals()
    flaw = [blocking("f1", "prop-b")]
    design.seal_critiques(a_on_b=critique("c1", "prop-b", design.context_hash, flaw))
    runner = design.runner
    runner.state.current, runner.state.status = "H1", Status.AWAITING_HUMAN
    runner._save()
    return design


@pytest.mark.parametrize(("guard", "terminal"), [("reject", "T-REJECTED"), ("halt", "T-HALTED")])
def test_a_human_can_end_a_run_whose_design_budget_ran_out_before_synthesis(
    repo: Path,  # noqa: F811
    tmp_path: Path,
    signer: Signer,
    guard: str,
    terminal: str,
) -> None:
    """A2 reaches H1 before A3 has produced a synthesis; that must not strand the run."""
    runner = h1_before_synthesis(repo, tmp_path, signer).runner
    evidence = runner.approval_evidence()
    assert evidence["incomplete"] == ["synthesis_sealed"] and evidence["survivors"] == []
    doc, sig = signed(runner, signer, guard, None)
    assert runner.decide(doc, sig).node == terminal


def test_no_candidate_can_be_approved_before_b1_has_run(
    repo: Path,  # noqa: F811
    tmp_path: Path,
    signer: Signer,
) -> None:
    design = h1_before_synthesis(repo, tmp_path, signer)
    runner = design.runner
    doc, sig = signed(runner, signer, APPROVE_GUARD, "proposal_a_sealed")
    with pytest.raises(ApprovalError, match="design is incomplete.*synthesis_sealed"):
        runner.decide(doc, sig)
    assert runner.state.current == "H1" and runner.state.status is Status.AWAITING_HUMAN


def test_a_run_that_is_not_waiting_for_a_human_has_no_evidence(tmp_path: Path) -> None:
    rig = Rig(tmp_path)
    runner = rig.open()
    with pytest.raises(ApprovalError, match="awaiting_human"):
        runner.approval_evidence()


def test_a_signed_revise_decision_can_enter_a_model_stage(
    tmp_path: Path, signer: Signer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """H1 -> A1 dispatches models; the signed decision must pass the human-state check there."""
    rig, runner = h1(tmp_path, signer, monkeypatch)
    doc, sig = signed(runner, signer, "revise_with_notes", None)
    result = runner.decide(doc, sig)
    assert (result.outcome, result.node) == (Outcome.ADVANCED, "A1")


def test_leaving_a_human_state_without_a_decision_is_still_denied(
    tmp_path: Path, signer: Signer, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig, runner = h1(tmp_path, signer, monkeypatch)
    edge = runner._edge("H1", "reject")
    runner.state.status = Status.RUNNING
    result = runner._take("H1", "reject", edge)
    assert result.outcome is Outcome.BLOCKED and "recorded human decision" in result.detail


def test_the_approve_command_says_when_b1_has_not_run(
    repo: Path,  # noqa: F811
    tmp_path: Path,
    signer: Signer,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import argparse

    from agentic_pipeline import run_cli

    design = h1_before_synthesis(repo, tmp_path, signer)
    design.runner.close()
    ns = argparse.Namespace(
        root=repo,
        runs_dir=design.rig.run_dir.parent,
        run_id=design.rig.run_dir.name,
        decision="reject",
        approver="alice",
        reason="the design never converged",
        candidate=None,
        remediation_cause=None,
        out=tmp_path / "doc.json",
        run_command="approve",
    )
    assert run_cli.run_command(ns) == 0
    shown = capsys.readouterr().out
    assert "design is incomplete (never produced: ['synthesis_sealed'])" in shown
    assert "survived B1" not in shown


def test_cli_approve_then_decide_round_trip(
    tmp_path: Path,
    signer: Signer,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import argparse

    from agentic_pipeline import run_cli

    rig, runner = h1(tmp_path, signer, monkeypatch)
    runner.close()
    common = {"root": rig.root, "runs_dir": tmp_path, "run_id": "run-1"}
    out = tmp_path / "doc.json"
    ns = argparse.Namespace(
        **common,
        decision="reject",
        approver="alice",
        reason="not now",
        candidate=None,
        remediation_cause=None,
        out=out,
        run_command="approve",
    )
    assert run_cli.run_command(ns) == 0
    shown = capsys.readouterr().out
    assert f"ssh-keygen -Y sign -f ~/.ssh/id_ed25519 -n {NAMESPACE} {out}" in shown
    assert "proposal_b_sealed" in shown
    out.with_name("doc.json.sig").write_text(signer.sign(out.read_bytes()))
    decide = argparse.Namespace(**common, approval=out, signature=None, run_command="decide")
    assert run_cli.run_command(decide) == 0
    assert stored(rig).current == "T-REJECTED"
    # Reusing the file, or a missing signature, is an error with exit 1 and no change.
    assert run_cli.run_command(decide) == 1
    out.with_name("doc.json.sig").unlink()
    assert run_cli.run_command(decide) == 1


def seal_candidate(rig: Rig, runner: Any, stage: str, artifact: str, operation: str) -> Path:
    """Publish a sealed design candidate the way the design stages would."""
    from agentic_pipeline.manifest import build_manifest, publish_stage_manifest

    folder = rig.run_dir / "attempts" / f"{stage}-test" / operation
    folder.mkdir(parents=True)
    (folder / f"{operation}.json").write_text(json.dumps({"id": operation, "topology": "t"}))
    manifest = build_manifest(
        rig.run_dir,
        run_id=runner.state.run_id,
        stage=stage,
        attempt=f"{stage}-test",
        artifacts={artifact: (folder, True)},
        operations={},
        provenance_tip=runner.log.tip(),
        data_classes={artifact: "internal"},
    )
    publish_stage_manifest(rig.run_dir, manifest)
    return folder / f"{operation}.json"


def approved_at_dr(rig: Rig, runner: Any, signer: Signer) -> bytes:
    runner.log.append({"state": "H1", "event": "enter"})  # `rig.at` skips the real entry
    doc, sig = signed(runner, signer, APPROVE_GUARD, "proposal_b_sealed")
    assert runner.decide(doc, sig).node == "DR"
    return doc


def test_dr_seals_the_approved_candidate_with_its_content_for_the_build(
    tmp_path: Path, signer: Signer, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agentic_pipeline.manifest import resolve_sealed_inputs
    from agentic_pipeline.schemas import validate

    rig, runner = h1(tmp_path, signer, monkeypatch)
    sealed_file = seal_candidate(rig, runner, "A1", "proposal_b_sealed", "proposal_b")
    doc = approved_at_dr(rig, runner, signer)
    result = runner.step()  # DR runs; C1 then lacks only what this rig never sealed
    assert "design_decision_sealed" not in result.detail, result.detail
    assert "synthesis_sealed" in result.detail  # the rig has no synthesis behind it
    sealed, errors = resolve_sealed_inputs(
        rig.run_dir, runner.state.run_id, runner.registry, ["design_decision_sealed"]
    )
    assert not errors, errors
    folder = sealed["design_decision_sealed"].path
    decision = json.loads((folder / "decision.json").read_text())
    validate("design_decision", decision)
    assert decision["candidate"] == "proposal_b_sealed" and decision["source"] == "human_approval"
    assert decision["approver"] == "alice" and decision["approval_digest"] == approvals.digest(doc)
    # the build gets the approved design itself, byte for byte
    assert (folder / "candidate.json").read_bytes() == sealed_file.read_bytes()
    assert decision["candidate_digest"] == approvals.digest(
        (folder / "candidate.json").read_bytes()
    )
    assert verify_provenance_chain(rig.log.path).ok


def test_dr_blocks_when_the_approved_candidate_is_not_sealed(
    tmp_path: Path, signer: Signer, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig, runner = h1(tmp_path, signer, monkeypatch)
    approved_at_dr(rig, runner, signer)  # nothing was ever sealed for proposal_b_sealed
    result = runner.step()
    assert result.outcome is Outcome.BLOCKED and "proposal_b_sealed" in result.detail
    assert stored(rig).current == "DR"


def test_dr_blocks_when_the_approved_candidate_changed_after_it_was_sealed(
    tmp_path: Path, signer: Signer, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig, runner = h1(tmp_path, signer, monkeypatch)
    sealed_file = seal_candidate(rig, runner, "A1", "proposal_b_sealed", "proposal_b")
    approved_at_dr(rig, runner, signer)
    sealed_file.write_text("{}")
    result = runner.step()
    assert result.outcome is Outcome.BLOCKED and "content changed" in result.detail


def test_dr_refuses_a_stored_approval_that_was_changed(
    tmp_path: Path, signer: Signer, monkeypatch: pytest.MonkeyPatch
) -> None:
    rig, runner = h1(tmp_path, signer, monkeypatch)
    seal_candidate(rig, runner, "A1", "proposal_b_sealed", "proposal_b")
    doc = approved_at_dr(rig, runner, signer)
    stored_doc = rig.run_dir / "approvals" / f"{approvals.digest(doc).removeprefix('sha256:')}.json"
    stored_doc.write_bytes(doc.replace(b"proposal_b_sealed", b"proposal_a_sealed"))
    result = runner.step()
    assert (
        result.outcome is Outcome.BLOCKED and "does not match its recorded digest" in result.detail
    )
    assert stored(rig).current == "DR"


def test_dr_on_the_fast_path_still_needs_a_scored_winner(tmp_path: Path) -> None:
    runner = Rig(tmp_path).at("DR")  # not entered from H1
    result = runner.step()
    assert result.outcome is Outcome.BLOCKED and "none is ever invented" in result.detail
