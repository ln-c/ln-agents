"""The readable H1 report, rendered from the replayed deploy-design run (no model calls)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from playback import Recording
from test_grounding import repo  # noqa: F401  (repo is a fixture)
from test_playback import Replay, copy_recording, edit, replay  # noqa: F401

from agentic_pipeline import evidence_md
from agentic_pipeline.a2a_server import PipelineService
from agentic_pipeline.approvals import APPROVE_GUARD, REMEDIATION_CAUSES
from agentic_pipeline.run_cli import evidence_report


def view_of(replay: Replay) -> dict[str, Any]:  # noqa: F811
    result, runner = replay.run()
    try:
        assert result.node == "H1", result.detail
        return runner.approval_view()
    finally:
        runner.close()


def test_the_report_shows_every_choice_candidate_finding_and_digest(replay: Replay) -> None:  # noqa: F811
    view = view_of(replay)
    report = view["markdown"]
    approve = [c["candidate"] for c in view["choices"] if c["decision"] == APPROVE_GUARD]
    assert approve == view["evidence"]["survivors"]
    others = [c["decision"] for c in view["choices"] if c["decision"] != APPROVE_GUARD]
    assert others == [
        "request_evidence",
        "revise_with_notes",
        "approve_remediation_or_constraint_change",
        "reject",
        "halt",
    ]
    assert view["approvers"] == ["ln-c"]
    for choice in view["choices"]:
        assert f"**{choice['label']}**" in report
    for heading in ("## Design A", "## Design B", "## The synthesis", "## Findings"):
        assert heading in report
    for key, value in view["evidence"].items():
        assert f"- `{key}`:" in report
        if isinstance(value, str):
            assert value in report  # every digest a signature binds to is shown
    for stage, operation in (("A2", "critique_b_by_a"), ("A2", "critique_a_by_b")):
        findings = replay.recording.output(f"{stage}/{operation}")["findings"]
        for finding in findings:
            assert f"**{finding['id']}**" in report
    assert "valid (p = " in report and "| 1 |" in report  # verdicts and the ranking


def test_the_report_is_the_same_every_time(replay: Replay) -> None:  # noqa: F811
    first = view_of(replay)["markdown"]
    runner = replay.open()
    try:
        assert runner.approval_view()["markdown"] == first
    finally:
        runner.close()


def test_model_text_cannot_become_a_link_or_html(repo: Path, tmp_path: Path) -> None:  # noqa: F811
    fixtures = copy_recording(tmp_path)

    def plant(document: dict[str, Any]) -> None:
        document["topology"]["components"][0]["name"] = "[approve now](https://evil) <b>x</b>"

    edit(fixtures / "deploy-design" / "A1" / "proposal_a.json", plant)
    replay = Replay(repo, tmp_path, Recording.load("deploy-design", fixtures))  # noqa: F811
    report = view_of(replay)["markdown"]
    assert "\\[approve now\\](https://evil) \\<b\\>x\\</b\\>" in report
    assert "[approve now](" not in report and "<b>" not in report


def test_the_cli_and_the_a2a_skill_write_the_report(replay: Replay, repo: Path) -> None:  # noqa: F811
    view_of(replay)
    view, path = evidence_report(repo, replay.run_dir)
    assert path == replay.run_dir / "approvals" / "H1-evidence.md"
    assert path.read_text(encoding="utf-8") == view["markdown"]
    path.unlink()

    service = PipelineService(repo, replay.run_dir.parent)  # reading evidence dispatches nothing
    reply = service.evidence(replay.recording.run_id)
    assert reply["markdown"] == view["markdown"] and Path(reply["path"]) == path
    assert path.is_file()
    json.dumps(reply)  # what the skill returns must be plain JSON


def test_the_approval_document_skill_names_where_and_under_what_namespace_to_sign(
    replay: Replay,  # noqa: F811
    repo: Path,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    view = view_of(replay)
    choice = next(c for c in view["choices"] if c["decision"] == APPROVE_GUARD)
    monkeypatch.chdir(replay.run_dir.parent)
    service = PipelineService(repo, Path("."))  # a relative runs directory
    reply = service.approval_document(
        replay.recording.run_id,
        decision=choice["decision"],
        approver="ln-c",
        reason="looks right",
        candidate=choice["candidate"],
    )
    document = Path(reply["document"])
    assert document.is_absolute() and document.is_file()
    # ln-hub opens the signing terminal from exactly this layout and namespace
    assert document.parent == (replay.run_dir / "approvals").resolve()
    assert reply["namespace"] == "agentic-pipeline-approval"
    # the by-hand commands name the same absolute document, however the server was started
    assert reply["namespace"] in reply["sign"] and str(document) in reply["sign"]
    assert str(document) in reply["then"]
    json.dumps(reply)


@pytest.mark.parametrize(
    ("node", "guards", "expected"),
    [
        ("R1", ["approve", "deny"], ["approve", *(["deny"] * len(REMEDIATION_CAUSES))]),
        ("R2", ["approve", "deny_or_not_requested"], ["approve", "deny_or_not_requested"]),
    ],
)
def test_choices_name_a_cause_where_a_decision_needs_one(
    node: str, guards: list[str], expected: list[str]
) -> None:
    offered = evidence_md.choices(node, guards, {"survivors": []})
    assert [c["decision"] for c in offered] == expected
    causes = [c["remediation_cause"] for c in offered if c["remediation_cause"]]
    assert causes == ([*REMEDIATION_CAUSES] if node == "R1" else [])
