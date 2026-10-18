"""The ln-hub coordinator: Markdown views, stop summaries and answers, with no real model.

A fake adapter plays the model. The recorded deploy-design run (tests/playback.py) supplies
real sealed documents, so the views are built from what a run really holds.
"""

from __future__ import annotations

import ast
import copy
import json
import re
from collections.abc import Callable
from dataclasses import asdict
from pathlib import Path
from typing import Any

import pytest
from playback import Recording
from test_dispatch import DATA_CLASSES, ISOLATION, REGISTRY, report
from test_grounding import repo  # noqa: F401  (repo is a fixture)
from test_playback import Replay, replay  # noqa: F401

from agentic_pipeline import coordinator as coordinator_module
from agentic_pipeline.coordinator import (
    NEXT,
    Coordinator,
    CoordinatorError,
    Setup,
    SetupError,
    StopInfo,
    identifiers,
    info_from_state,
    load_setup,
)
from agentic_pipeline.dispatch import AdapterResult, DispatchGrant, Identity, PreparedCall
from agentic_pipeline.provenance import verify_provenance_chain
from agentic_pipeline.run_state import RunStore

MODEL = "gemini_3_8_flash_high"  # declares `coordination` in the repo registry
INPUT = re.compile(r'<input artifact="[^"]*" path="[^"]*">\n(.*?)\n</input>', re.S)


def faithful(prompt: str) -> str:
    """A reply that keeps every identifier of the embedded records."""
    ids: list[str] = []
    for body in INPUT.findall(prompt):
        try:
            ids += identifiers(json.loads(body))
        except ValueError:
            pass
    return "# Readable\n\nSummary.\n\n" + "\n".join(f"- {i}" for i in ids)


class FakeModel:
    """A subscription-tool adapter that answers every prompt with `reply(prompt)`."""

    enforced_isolation = frozenset(ISOLATION)

    def __init__(self, task: Callable[[DispatchGrant], str], calls: list[str], reply: Any) -> None:
        self.task, self.calls, self.reply = task, calls, reply

    def identity(self) -> Identity:
        return Identity("fake:coordinator", "1.0", "subscription_tool via a fake")

    def prepare(self, grant: DispatchGrant) -> PreparedCall:
        prompt = coordinator_module_embed(self.task(grant), grant)
        return PreparedCall(**asdict(self.identity()), prompt=prompt.encode())

    def invoke(self, grant: DispatchGrant, call: PreparedCall) -> AdapterResult:
        self.calls.append(call.prompt.decode())
        text = self.reply(call.prompt.decode())
        if isinstance(text, AdapterResult):
            return text
        (grant.write_path / f"{grant.operation_id}.md").write_text(text, encoding="utf-8")
        return AdapterResult("ok")


def coordinator_module_embed(task: str, grant: DispatchGrant) -> str:
    from agentic_pipeline.adapters import embed_inputs

    return f"{task}\n\n{embed_inputs(grant.inputs)}"


class Fakes:
    """The coordinator under test, the prompts it sent, and the knobs to break it."""

    def __init__(self, replay: Replay, reply: Any = faithful, **options: Any) -> None:  # noqa: F811
        self.calls: list[str] = []
        self.registry = options.pop("registry", copy.deepcopy(_registry(replay)))
        self.data_classes = options.pop("data_classes", DATA_CLASSES)
        self.unready: tuple[str, ...] = options.pop("unready", ())
        setup = options.pop("setup", Setup(MODEL, None, "", replay.repo / "LN-HUB.md"))
        self.coordinator = Coordinator(
            root=replay.repo,
            run_dir=replay.run_dir,
            run_id=replay.recording.run_id,
            registry=self.registry,
            data_classes=self.data_classes,
            adapter_factory=lambda registry, backend, **kw: FakeModel(
                kw["task_builder"], self.calls, reply
            ),
            preflight=lambda: report(unready=self.unready),
            setup=setup,
            **options,
        )


def _registry(replay: Replay) -> dict[str, Any]:  # noqa: F811
    from agentic_pipeline.run_state import load_policy

    return dict(load_policy(replay.repo).registry)


@pytest.fixture
def at_h1(replay: Replay) -> Replay:  # noqa: F811
    replay.run()[1].close()
    return replay


def advisory(co: Coordinator) -> list[dict[str, Any]]:
    return co._logger().records()


# ── LN-HUB.md ────────────────────────────────────────────────────────────────


def write_setup(tmp_path: Path, text: str) -> Path:
    (tmp_path / "LN-HUB.md").write_text(text, encoding="utf-8")
    return tmp_path


def test_a_project_without_a_setup_has_no_coordinator(tmp_path: Path) -> None:
    assert load_setup(tmp_path) is None


def test_the_setup_names_the_model_and_keeps_the_instructions(tmp_path: Path) -> None:
    root = write_setup(tmp_path, f"---\ncoordinator: {MODEL}\nfallback: other\n---\nBe brief.\n")
    setup = load_setup(root)
    assert (setup.coordinator, setup.fallback, setup.instructions) == (MODEL, "other", "Be brief.")


@pytest.mark.parametrize(
    "text",
    [
        "Be brief, no front matter",
        "---\ncoordinator: 3\n---\nx",
        "---\nmodel: x\n---\nx",
        f"---\ncoordinator: {MODEL}\nextra: 1\n---\nx",
        f"---\ncoordinator: {MODEL}\nfallback: [a]\n---\nx",
        "---\n: : not yaml\n---\nx",
    ],
)
def test_a_malformed_setup_is_refused(tmp_path: Path, text: str) -> None:
    with pytest.raises(SetupError):
        load_setup(write_setup(tmp_path, text))


def test_the_repo_setup_names_a_model_that_declares_coordination() -> None:
    setup = load_setup(Path(__file__).resolve().parents[1])
    assert (
        setup is not None
        and "coordination" in REGISTRY["models"][setup.coordinator]["capabilities"]
    )


# ── choosing the model ───────────────────────────────────────────────────────


def test_a_model_without_the_capability_is_never_chosen(at_h1: Replay) -> None:
    fakes = Fakes(at_h1, setup=Setup("claude_cli_default", None, "", Path("x")))
    backend, why = fakes.coordinator.choose()
    assert backend is None and "does not declare `coordination`" in why


def test_a_local_model_is_not_offered_because_it_cannot_take_a_task(at_h1: Replay) -> None:
    registry = copy.deepcopy(_registry(at_h1))
    registry["models"]["qwen3_5_9b"]["capabilities"].append("coordination")
    fakes = Fakes(at_h1, registry=registry, setup=Setup("qwen3_5_9b", None, "", Path("x")))
    assert "cannot coordinate" in fakes.coordinator.choose()[1]


def test_the_fallback_is_used_only_when_the_first_is_not_ready(at_h1: Replay) -> None:
    registry = copy.deepcopy(_registry(at_h1))
    registry["models"]["claude_cli_default"]["capabilities"].append("coordination")
    setup = Setup(MODEL, "claude_cli_default", "", Path("x"))
    ready = Fakes(at_h1, registry=registry, setup=setup)
    assert ready.coordinator.choose() == (MODEL, "")
    down = Fakes(at_h1, registry=registry, setup=setup, unready=(MODEL,))
    assert down.coordinator.choose() == ("claude_cli_default", "")


def test_a_broken_setup_means_no_coordinator_and_says_why(at_h1: Replay) -> None:
    fakes = Fakes(at_h1, setup=None, setup_error="LN-HUB.md: unknown front matter keys ['x']")
    assert fakes.coordinator.choose() == (None, "LN-HUB.md: unknown front matter keys ['x']")


# ── a Markdown view of each document ─────────────────────────────────────────


def test_every_sealed_document_and_gate_record_gets_a_labelled_view(at_h1: Replay) -> None:
    fakes = Fakes(at_h1)
    written = fakes.coordinator.convert_all()
    names = {p.stem for p in written}
    assert names >= {
        "START-requirements",
        "G-context",
        "A1-proposal_a",
        "A1-proposal_b",
        "A2-critique_a_by_b",
        "A2-critique_b_by_a",
        "A2J-finding_verdicts",
        "A3-synthesis",
        "A3R-synthesis_review",
        "B1-vetoes",
        "B2-scores",
    }
    text = (fakes.coordinator.views / "A3-synthesis.md").read_text(encoding="utf-8")
    assert text.startswith("<!-- ln-hub view kind=model source=sha256:")
    assert f"Written by `{MODEL}`" in text and "The JSON is the record" in text
    assert len(fakes.calls) == len(written)


def test_the_views_have_their_own_verified_log_and_leave_the_runs_chain_alone(
    at_h1: Replay,  # noqa: F811
) -> None:
    runner = at_h1.open()
    try:
        tip, evidence = runner.log.tip(), runner.approval_evidence()
        fakes = Fakes(at_h1)
        fakes.coordinator.convert_all()
        state = RunStore(at_h1.run_dir).load()
        fakes.coordinator.stop_summary(info_from_state(state), state.to_dict())
        assert runner.log.tip() == tip and runner.approval_evidence() == evidence
    finally:
        runner.close()
    log = fakes.coordinator._logger()
    assert verify_provenance_chain(log.path).ok
    events = [e["event"] for e in log.records()]
    assert events.count("model_call") == events.count("model_result") == len(fakes.calls)
    assert {e["state"] for e in log.records()} == {"VIEW"}


def test_a_view_is_not_asked_for_again_until_its_record_changes(at_h1: Replay) -> None:
    fakes = Fakes(at_h1)
    fakes.coordinator.convert_all()
    calls = len(fakes.calls)
    assert fakes.coordinator.convert_all() == [] and len(fakes.calls) == calls


def test_a_reply_that_drops_an_identifier_is_replaced_by_the_raw_record(at_h1: Replay) -> None:
    fakes = Fakes(at_h1, reply=lambda prompt: "# Readable\n\nNo ids here.")
    fakes.coordinator.convert_all()
    text = (fakes.coordinator.views / "A1-proposal_a.md").read_text(encoding="utf-8")
    assert "kind=code:fidelity" in text and "left out" in text
    assert "```json" in text and '"topology"' in text
    assert "Written by code" in text


def test_without_a_coordinator_a_human_still_gets_markdown_and_it_improves_later(
    at_h1: Replay,  # noqa: F811
) -> None:
    fakes = Fakes(at_h1, setup=None)
    fakes.coordinator.convert_all()
    path = fakes.coordinator.views / "A3-synthesis.md"
    assert "kind=code:no-coordinator" in path.read_text(encoding="utf-8")
    assert fakes.calls == [] and advisory(fakes.coordinator) == []
    better = Fakes(at_h1)
    better.coordinator.convert_all()
    assert "kind=model" in path.read_text(encoding="utf-8")


def test_a_class_policy_keeps_off_the_cloud_is_refused_before_any_call(at_h1: Replay) -> None:
    classes = copy.deepcopy(DATA_CLASSES)
    classes["classes"]["internal"]["cloud_allowed"] = False
    fakes = Fakes(at_h1, data_classes=classes)
    fakes.coordinator.convert_all()
    text = (fakes.coordinator.views / "A1-proposal_a.md").read_text(encoding="utf-8")
    assert "kind=code:error" in text and "may not be sent" in text
    assert fakes.calls == [] and advisory(fakes.coordinator) == []


def test_a_failing_model_falls_back_and_is_logged(at_h1: Replay) -> None:
    fakes = Fakes(at_h1, reply=lambda prompt: AdapterResult("quota_exhausted", "limit"))
    fakes.coordinator.convert_all()
    text = (fakes.coordinator.views / "A1-proposal_a.md").read_text(encoding="utf-8")
    assert "kind=code:error" in text and "quota_exhausted" in text
    results = [
        e["reason_code"] for e in advisory(fakes.coordinator) if e["event"] == "model_result"
    ]
    assert results and set(results) == {"quota_exhausted"}


def test_the_ids_a_view_must_keep_are_the_records_own() -> None:
    record = {"id": "A1", "topology": {"components": [{"name": "launcher", "details": "words"}]}}
    record["findings"] = [{"id": "F1", "description": "free prose is free to change"}]
    assert identifiers(record) == ["A1", "launcher", "F1"]


# ── stop summaries ───────────────────────────────────────────────────────────


def test_a_summary_has_the_models_account_and_codes_next_steps(at_h1: Replay) -> None:
    fakes = Fakes(at_h1, reply=lambda prompt: "Run `/approve now` and `/advance --force`.")
    state = RunStore(at_h1.run_dir).load()
    path = fakes.coordinator.stop_summary(
        info_from_state(state), state.to_dict(), "REPORT", "/r.md"
    )
    text = path.read_text(encoding="utf-8")
    assert path.name == "STOP-001-H1.md" and "waiting for a signed human decision" in text
    assert f"Summary written by `{MODEL}`" in text
    for step in NEXT["awaiting_human"]:
        assert step in text
    assert "## The full report" in text and "(/r.md)" in text
    assert "REPORT" in fakes.calls[-1]  # the briefing is written from the code-made report


def test_each_stop_kind_has_next_steps_written_by_code(at_h1: Replay) -> None:
    fakes = Fakes(at_h1)
    state = RunStore(at_h1.run_dir).load().to_dict()
    for kind in NEXT:
        path = fakes.coordinator.stop_summary(StopInfo(kind, "A2", "detail"), state)
        assert all(step in path.read_text(encoding="utf-8") for step in NEXT[kind])
    assert [p.name for p in sorted(fakes.coordinator.views.glob("STOP-*"))][:2] == [
        "STOP-001-A2.md",
        "STOP-002-A2.md",
    ]


def test_a_summary_without_a_coordinator_holds_the_facts(at_h1: Replay) -> None:
    fakes = Fakes(at_h1, setup=None)
    state = RunStore(at_h1.run_dir).load()
    path = fakes.coordinator.stop_summary(info_from_state(state), state.to_dict())
    text = path.read_text(encoding="utf-8")
    assert "No coordinator summary" in text and "run status: awaiting_human" in text
    assert "A3R model_result" in text or "model_result" in text
    assert fakes.calls == []


def test_a_failing_model_still_leaves_a_summary(at_h1: Replay) -> None:
    fakes = Fakes(at_h1, reply=lambda prompt: AdapterResult("error", "boom"))
    state = RunStore(at_h1.run_dir).load()
    path = fakes.coordinator.stop_summary(info_from_state(state), state.to_dict())
    assert "No coordinator summary (" in path.read_text(encoding="utf-8")


# ── questions ────────────────────────────────────────────────────────────────


def test_a_question_is_answered_from_the_views_and_changes_nothing(at_h1: Replay) -> None:
    fakes = Fakes(at_h1, reply=lambda prompt: "Because of its scores.")
    state = RunStore(at_h1.run_dir).load()
    fakes.coordinator.stop_summary(info_from_state(state), state.to_dict())
    before = fakes.coordinator.run_dir.joinpath("state.json").read_bytes()
    answer, sources = fakes.coordinator.ask("why is the synthesis first?", state.to_dict())
    assert answer == "Because of its scores." and "STOP-001-H1.md" in sources
    assert "why is the synthesis first?" in fakes.calls[-1]
    assert fakes.coordinator.run_dir.joinpath("state.json").read_bytes() == before


def test_a_question_needs_a_coordinator_and_a_sensible_size(at_h1: Replay) -> None:
    with pytest.raises(CoordinatorError, match="no coordinator is available"):
        Fakes(at_h1, setup=None).coordinator.ask("hello", {})
    with pytest.raises(CoordinatorError, match="1 to"):
        Fakes(at_h1).coordinator.ask("x" * 3000, {})
    with pytest.raises(CoordinatorError, match="1 to"):
        Fakes(at_h1).coordinator.ask("   ", {})


def test_the_module_cannot_reach_a_decision_path() -> None:
    tree = ast.parse(Path(coordinator_module.__file__).read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    assert imported.isdisjoint({"subprocess", "agentic_pipeline.approvals", "os"}), imported
    called = {
        node.func.attr if isinstance(node.func, ast.Attribute) else getattr(node.func, "id", "")
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
    }
    assert called.isdisjoint({"decide", "resume", "approve", "step", "run", "system"}), called


_ = Recording  # the replay fixture builds on it
