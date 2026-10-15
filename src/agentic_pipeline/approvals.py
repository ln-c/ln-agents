"""Signed human approvals for the human gates.

A human decision is a canonical JSON document signed with `ssh-keygen -Y sign` in the
namespace from `policy/approvers.yaml`. The runner verifies it offline with
`ssh-keygen -Y verify` against the keys in that file, then accepts it only if:

* the signer is listed and may make this kind of decision (`design` for H1);
* the document names this run, this node and one of the node's outgoing guards;
* the evidence it names equals the evidence recomputed from sealed state now. That evidence
  includes the provenance tip and the frozen policy digests, so an approval cannot be reused
  after anything has changed, for another run, or after it has been used once (taking the
  edge appends to the log, which moves the tip);
* for the design approval, the chosen candidate is a B1 survivor. A candidate with a fired
  veto can never be approved. Vetoes that were undetermined are listed in the evidence the
  approver signs, so signing is the human attestation for them.

Only H1 (design), R1 (merge) and R2 (deploy) are wired; no deployment environment exists
yet, so R2 can only deny or decline. A model cannot make a decision: it holds no listed key,
and the runner rejects anything that does not verify. The private key's protection is the
user's host configuration; a key readable by a model's process voids that guarantee.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import tempfile
from collections.abc import Collection, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

KEY_TYPES = frozenset({"ssh-ed25519", "sk-ssh-ed25519@openssh.com"})
DECISION_TYPES = ("design", "merge", "deploy", "calibration", "adr")
NODE_DECISION = {"H1": "design", "R1": "merge", "R2": "deploy"}  # human states wired
REMEDIATION_CAUSES = ("code_defect", "test_defect", "design_issue")  # R1 deny -> RM routes
MERGE_APPROVE_GUARD = "approve"
APPROVE_GUARD = "approve_design_and_no_security_veto"
DOCUMENT_SCHEMA = "agentic-pipeline/approval/v2"
FIELDS = frozenset(
    {
        "schema",
        "run_id",
        "node",
        "decision",
        "decision_type",
        "approver",
        "candidate",
        "remediation_cause",
        "reason",
        "evidence",
        "issued_at",
    }
)
SIGN_TIMEOUT_SECONDS = 20


class ApprovalError(Exception):
    """An approval is missing, malformed, unsigned, unauthorized, or stale: nothing changes."""


@dataclass(frozen=True, slots=True)
class Verified:
    approver: str
    guard: str
    candidate: str | None
    reason: str
    digest: str  # sha256 of the signed document bytes
    approved_sha: str  # what the decision binds to: the pinned commit (H1) or candidate tree (R1)


def canonical(document: Mapping[str, Any]) -> bytes:
    return (
        json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False) + "\n"
    ).encode()


def digest(document_bytes: bytes) -> str:
    return "sha256:" + hashlib.sha256(document_bytes).hexdigest()


def build_document(
    *,
    run_id: str,
    node: str,
    guard: str,
    approver: str,
    candidate: str | None,
    reason: str,
    evidence: Mapping[str, Any],
    issued_at: str,
    remediation_cause: str | None = None,
) -> bytes:
    if node not in NODE_DECISION:
        raise ApprovalError(f"signed approvals are not wired for {node} yet")
    return canonical(
        {
            "schema": DOCUMENT_SCHEMA,
            "run_id": run_id,
            "node": node,
            "decision": guard,
            "decision_type": NODE_DECISION[node],
            "approver": approver,
            "candidate": candidate,
            "remediation_cause": remediation_cause,
            "reason": reason,
            "evidence": evidence,
            "issued_at": issued_at,
        }
    )


def signing_command(document: Path, namespace: str) -> str:
    return f"ssh-keygen -Y sign -f ~/.ssh/id_ed25519 -n {namespace} {document}"


def _verify_signature(
    document_bytes: bytes, signature: str, approver: Mapping[str, Any], namespace: str
) -> None:
    tool = shutil.which("ssh-keygen")
    if tool is None:
        raise ApprovalError("ssh-keygen is not installed, so no signature can be verified")
    line = f'{approver["id"]} namespaces="{namespace}" {approver["public_key"]}\n'
    with tempfile.TemporaryDirectory(prefix="approval-") as tmp:
        allowed, sig = Path(tmp, "allowed_signers"), Path(tmp, "message.sig")
        allowed.write_text(line, encoding="utf-8")
        sig.write_text(signature, encoding="utf-8")
        try:
            done = subprocess.run(  # noqa: S603 - fixed argv, no shell, 20s timeout
                [
                    tool,
                    "-Y",
                    "verify",
                    "-f",
                    str(allowed),
                    "-I",
                    approver["id"],
                    "-n",
                    namespace,
                    "-s",
                    str(sig),
                ],
                input=document_bytes,
                capture_output=True,
                timeout=SIGN_TIMEOUT_SECONDS,
                check=False,
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise ApprovalError(f"cannot run ssh-keygen to verify the signature: {exc}") from exc
    if done.returncode != 0:
        raise ApprovalError("signature does not verify against the listed key")


def parse_signed(
    document_bytes: bytes, signature: str, *, approvers: Mapping[str, Any], node: str
) -> dict[str, Any]:
    """The document if it is canonical, signed by a listed approver who may decide at `node`."""
    if node not in NODE_DECISION:
        raise ApprovalError(f"signed approvals are not wired for {node} yet")
    try:
        doc = json.loads(document_bytes)
    except ValueError as exc:
        raise ApprovalError(f"approval document is not JSON: {exc}") from exc
    if not isinstance(doc, dict) or set(doc) != FIELDS or doc["schema"] != DOCUMENT_SCHEMA:
        raise ApprovalError("approval document does not have the expected fields")
    if canonical(doc) != document_bytes:
        raise ApprovalError("approval document is not in canonical form; regenerate it")
    listed = {a["id"]: a for a in approvers.get("approvers", [])}
    who = listed.get(doc["approver"])
    if who is None:
        raise ApprovalError(f"approver {doc['approver']!r} is not listed in policy/approvers.yaml")
    needed = NODE_DECISION[node]
    if needed not in who["decisions"] or doc["decision_type"] != needed:
        raise ApprovalError(f"{doc['approver']} may not make a {needed!r} decision")
    _verify_signature(document_bytes, signature, who, approvers["namespace"])
    return doc


def store_approval(run_root: Path, document: bytes, signature: str) -> Path:
    """Keep the signed document in the run so a later gate (M1) can verify it again."""
    folder = run_root / "approvals"
    folder.mkdir(exist_ok=True)
    path = folder / f"{digest(document).removeprefix('sha256:')}.json"
    path.write_bytes(document)
    path.with_name(path.name + ".sig").write_text(signature, encoding="utf-8")
    return path


def verify_approval(
    document_bytes: bytes,
    signature: str,
    *,
    approvers: Mapping[str, Any],
    run_id: str,
    node: str,
    guards: Collection[str],
    evidence: Mapping[str, Any],
) -> Verified:
    """Accept a signed approval for this run/node against freshly recomputed `evidence`."""
    doc = parse_signed(document_bytes, signature, approvers=approvers, node=node)
    if doc["run_id"] != run_id or doc["node"] != node:
        raise ApprovalError(f"approval is for {doc['run_id']}/{doc['node']}, not {run_id}/{node}")
    if doc["decision"] not in guards:
        raise ApprovalError(f"{doc['decision']!r} is not a decision available at {node}")
    if not isinstance(doc["reason"], str) or not doc["reason"].strip():
        raise ApprovalError("an approval needs a reason")
    if doc["evidence"] != evidence:
        raise ApprovalError("the evidence changed since this was issued; issue a new approval")
    candidate = doc["candidate"]
    if node == "H1" and doc["decision"] == APPROVE_GUARD:
        if candidate not in evidence["survivors"]:
            raise ApprovalError(
                f"{candidate!r} is not a B1 survivor ({evidence['survivors']}); "
                "a candidate with a fired veto cannot be approved"
            )
    elif candidate is not None:
        raise ApprovalError("only a design approval names a candidate")
    cause = doc["remediation_cause"]
    if node == "R1" and doc["decision"] == "deny":
        if cause not in REMEDIATION_CAUSES:
            raise ApprovalError(f"a merge denial must name a cause from {list(REMEDIATION_CAUSES)}")
    elif cause is not None:
        raise ApprovalError("only a merge denial names a remediation cause")
    if node == "R2":
        if evidence.get("separate_deploy_approver") and doc["approver"] == evidence.get(
            "merge_approver"
        ):
            raise ApprovalError("policy requires a different approver for deploy than for merge")
        if doc["decision"] == "approve" and not evidence.get("environments"):
            raise ApprovalError(
                "no deployment environment is configured, so a deploy cannot be approved; "
                "sign deny_or_not_requested"
            )
    if node == "R1" and doc["decision"] == MERGE_APPROVE_GUARD:
        if evidence.get("f1_guard") != "all_required_pass":
            raise ApprovalError("a merge cannot be approved without passing F1 for this patch")
    return Verified(
        approver=doc["approver"],
        guard=doc["decision"],
        candidate=candidate,
        reason=doc["reason"],
        digest=digest(document_bytes),
        approved_sha=evidence.get("approved_sha", evidence["commit_sha"]),
    )
