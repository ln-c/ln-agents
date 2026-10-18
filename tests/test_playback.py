"""Replay the recorded deploy-design run through the real Runner, Dispatcher and gates.

No model is called: the Playback adapter (tests/playback.py) writes each recorded output where
the real adapter would have, and everything else (grounding, dispatch, sealing, provenance,
gates, H1 evidence) is the production code over a fresh git repository.
"""

from __future__ import annotations

import dataclasses
import json
import shutil
from pathlib import Path
from typing import Any

import pytest
from playback import FIXTURES, PlaybackAdapter, Recording
from test_grounding import git, repo  # noqa: F401  (repo is a fixture)

from agentic_pipeline.dispatch import Dispatcher
from agentic_pipeline.readiness import Check, Report, Status
from agentic_pipeline.run_cli import _adapter_factories
from agentic_pipeline.runner import Outcome, Runner, StepResult, init_run, open_run

ROOT = Path(__file__).resolve().parents[1]
RECORDED = ["A1/proposal_a", "A1/proposal_b", "A2/critique_b_by_a", "A2/critique_a_by_b"]
RECORDED += ["A2J/finding_verdicts", "A3/synthesis", "A3R/synthesis_review"]


def ready(registry: dict[str, Any]) -> Report:
    """Every provider and isolation check verified: readiness is not what is replayed."""
    ok = Status.VERIFIED
    checks = {c for profile in registry["isolation_profiles"].values() for c in profile}
    return Report(
        static_errors=[],
        global_checks=[Check("credential_env_absent", ok, "")],
        providers={p: [Check("executable", ok, "")] for p in registry["providers"]},
        isolation={c: Check(c, ok, "") for c in checks},
        registry=registry,
    )


class Replay:
    def __init__(self, repo: Path, tmp_path: Path, recording: Recording) -> None:  # noqa: F811
        self.repo = repo
        self.recording = recording
        self.adapters = recording.adapters()
        self.run_dir = tmp_path / "runs" / recording.run_id
        recording.pin_bindings(repo)
        git(repo, "commit", "-q", "-am", "bind the recorded backends")
        init_run(repo, self.run_dir, recording.run_id, "START", recording.entry_spec)

    def open(self) -> Runner:
        def make(policy: Any, log: Any, ledger: Any, guards: Any) -> Dispatcher:
            return Dispatcher(
                graph=policy.graph,
                registry=policy.registry,
                log=log,
                ledger=ledger,
                adapters=self.adapters,
                preflight=lambda: ready(policy.registry),
                run_root=self.run_dir,
                data_classes=policy.data_classes,
                guards=guards,
            )

        return open_run(self.repo, self.run_dir, make)

    def run(self) -> tuple[StepResult, Runner]:
        runner = self.open()
        return runner.run(), runner

    def record(self, node: str) -> dict[str, Any]:
        (path,) = (self.run_dir / "gates" / node).glob("*.json")
        return json.loads(path.read_text(encoding="utf-8"))

    def events(self, runner: Runner, kind: str) -> list[dict[str, Any]]:
        return [e for e in runner.log.records() if e["event"] == kind]


@pytest.fixture
def replay(repo: Path, tmp_path: Path) -> Replay:  # noqa: F811
    return Replay(repo, tmp_path, Recording.load("deploy-design"))


def recorded_gate(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / "deploy-design" / "gates" / name).read_text(encoding="utf-8"))


def test_the_recorded_design_replays_to_h1(replay: Replay) -> None:
    result, runner = replay.run()
    try:
        assert (result.outcome, result.node) == (Outcome.AWAITING_HUMAN, "H1"), result.detail
        calls = replay.events(runner, "model_call")
        assert [f"{e['state']}/{e['operation_id']}" for e in calls] == RECORDED
        assert all(e["access_method"].startswith("playback of deploy-design: ") for e in calls)
        results = replay.events(runner, "model_result")
        assert [e["reason_code"] for e in results] == ["ok"] * len(RECORDED)
        replayed = [key for adapter in replay.adapters.values() for key in adapter.replayed]
        assert sorted(replayed) == sorted(RECORDED)
    finally:
        runner.close()


def test_the_gates_decide_as_they_did_on_the_recorded_run(replay: Replay) -> None:
    replay.run()[1].close()
    vetoes, before = replay.record("B1"), recorded_gate("B1.json")
    for key in ("guard", "evaluated", "fired", "survivors", "undetermined", "unevaluated"):
        assert vetoes[key] == before[key], key
    route, before = replay.record("B3"), recorded_gate("B3.json")
    assert (route["guard"], route["require_all"]) == (before["guard"], before["require_all"])
    (path,) = (replay.run_dir / "gates" / "B2").glob("*/scores/scores.json")
    assert json.loads(path.read_text(encoding="utf-8")) == recorded_gate("B2-scores.json")


def test_h1_evidence_binds_the_replays_context_and_the_recorded_verdicts(replay: Replay) -> None:
    _, runner = replay.run()
    try:
        evidence = runner.approval_evidence()
    finally:
        runner.close()
    assert evidence["context_manifest"] != replay.recording.context_hash
    assert evidence["survivors"] == ["proposal_a_sealed", "proposal_b_sealed", "synthesis_sealed"]
    # Clef-Flash judged all 12 and 5 findings substantiated (lowest 0.35, threshold 0.1).
    assert evidence["valid_ratios"] == {
        "critique_a_by_b_sealed": 1.0,
        "critique_b_by_a_sealed": 1.0,
    }
    assert evidence["waived_blocking"] == []
    findings = replay.record("A2J")["findings"]
    judged = {f"{f['critique']}/{f['finding_id']}": f["probability"] for f in findings}
    verdicts = replay.recording.output("A2J/finding_verdicts")["results"]
    assert judged == {r["id"]: r["answers"]["valid"]["noul"] for r in verdicts}


# ── The playback adapter refuses what it cannot replay faithfully ────────────


def copy_recording(tmp_path: Path) -> Path:
    target = tmp_path / "fixtures"
    shutil.copytree(FIXTURES / "deploy-design", target / "deploy-design")
    return target


def edit(path: Path, change: Any) -> None:
    document = json.loads(path.read_text(encoding="utf-8"))
    change(document)
    path.write_text(json.dumps(document), encoding="utf-8")


def test_an_output_for_another_context_is_not_rebound(repo: Path, tmp_path: Path) -> None:  # noqa: F811
    fixtures = copy_recording(tmp_path)
    edit(
        fixtures / "deploy-design" / "A1" / "proposal_b.json",
        lambda d: d.update(manifest_hash="sha256:" + "0" * 64),
    )
    replay = Replay(repo, tmp_path, Recording.load("deploy-design", fixtures))
    result, runner = replay.run()
    runner.close()
    assert result.outcome is Outcome.PAUSED
    assert "manifest_hash is not the recorded context" in result.detail


def test_verdicts_for_other_findings_are_not_replayed(repo: Path, tmp_path: Path) -> None:  # noqa: F811
    fixtures = copy_recording(tmp_path)
    edit(fixtures / "deploy-design" / "A2J" / "finding_verdicts.json", lambda d: d["results"].pop())
    replay = Replay(repo, tmp_path, Recording.load("deploy-design", fixtures))
    result, runner = replay.run()
    runner.close()
    assert (result.outcome, result.node) == (Outcome.PAUSED, "A2")
    assert "recorded answers are not for these requests" in result.detail


def test_a_dispatch_to_another_backend_is_refused(replay: Replay) -> None:
    calls = dict(replay.recording.calls)
    calls["A1/proposal_a"] = {**calls["A1/proposal_a"], "backend": "gemini_3_8_flash_high"}
    moved = dataclasses.replace(replay.recording, calls=calls)
    replay.adapters["claude_cli_default"] = PlaybackAdapter(moved, "claude_cli_default")
    result, runner = replay.run()
    runner.close()
    assert result.outcome is Outcome.BLOCKED
    assert "recorded on gemini_3_8_flash_high, dispatched to claude_cli_default" in result.detail


# ── No real run can reach it ─────────────────────────────────────────────────


def test_no_real_run_path_can_construct_the_playback_adapter() -> None:
    """Nothing shipped or human-owned names it, and real adapters come only from adapters.py."""
    for folder in ("src", "policy", "pipeline", "schemas"):
        for path in sorted((ROOT / folder).rglob("*")):
            if path.is_file() and path.suffix in {".py", ".yaml", ".json"}:
                assert "playback" not in path.read_text(encoding="utf-8").lower(), path
    for factory in _adapter_factories().values():
        assert factory.__module__ == "agentic_pipeline.adapters"
