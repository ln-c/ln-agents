"""Tests for run state, recovery, and the runner (fake adapters and handlers only)."""

from __future__ import annotations

import copy
import json
import shutil
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import pytest
from test_dispatch import DATA_CLASSES, GRAPH, REGISTRY, FakeAdapter, report, seed

from agentic_pipeline.dispatch import Dispatcher
from agentic_pipeline.provenance import JsonlProvenanceLog, verify_provenance_chain
from agentic_pipeline.run_state import (
    PauseCause,
    PolicyDrift,
    PolicySnapshot,
    RunLocked,
    RunState,
    RunStateError,
    RunStore,
    Status,
    resume,
)
from agentic_pipeline.runner import ChosenGuard, Outcome, Runner, StateHandler, init_run, open_run
from agentic_pipeline.transition import BudgetLedger

ROOT = Path(__file__).resolve().parents[1]
RUN_ID = "run-1"
PRE_MODEL = ("START", "G")  # stages whose sealed outputs the model stages read
ALWAYS: StateHandler = lambda ctx: "always"  # noqa: E731
GROUNDING: dict[str, StateHandler] = {
    "G": ALWAYS,
    "START": lambda ctx: "task_class_valid",  # START's only edge is guarded, not `always`
}


class Rig:
    def __init__(self, tmp_path: Path, root: Path = ROOT) -> None:
        self.root = root
        self.run_dir = tmp_path / RUN_ID  # state.run_id must match the directory
        self.adapters = {
            b: FakeAdapter()
            for b in (
                "claude_cli_default",
                "gemini_3_8_flash_high",
                "qwen3_5_9b",
                "clef_flash_local",
            )
        }
        self.unready: tuple[str, ...] = ()
        seed(self.run_dir, PRE_MODEL)
        init_run(root, self.run_dir, RUN_ID, "START")

    def open(self, handlers: Mapping[str, StateHandler] | None = None) -> Runner:
        def make(
            policy: PolicySnapshot,
            log: JsonlProvenanceLog,
            ledger: BudgetLedger,
            guards: ChosenGuard,
        ) -> Dispatcher:
            self.policy_seen = policy
            return Dispatcher(
                graph=GRAPH,
                registry=REGISTRY,
                log=log,
                ledger=ledger,
                adapters=self.adapters,
                preflight=lambda: report(unready=self.unready),
                run_root=self.run_dir,
                data_classes=DATA_CLASSES,
                guards=guards,
            )

        return open_run(self.root, self.run_dir, make, handlers)

    def at(self, node: str, handlers: Mapping[str, StateHandler] | None = None) -> Runner:
        runner = self.open(handlers)
        runner.state.current = node
        runner._save()
        return runner

    @property
    def log(self) -> JsonlProvenanceLog:
        return JsonlProvenanceLog(self.run_dir / "provenance", RUN_ID)

    def events(self) -> list[dict[str, Any]]:
        return [json.loads(line) for line in self.log.path.read_text().splitlines()]


@pytest.fixture
def rig(tmp_path: Path) -> Rig:
    return Rig(tmp_path)


def stored(rig: Rig) -> RunState:
    return RunStore(rig.run_dir).load()


# ── Creating and storing runs ────────────────────────────────────────────────


def test_init_creates_state_and_first_provenance_event(rig: Rig) -> None:
    state = stored(rig)
    assert (state.current, state.status, state.steps) == ("START", Status.RUNNING, 0)
    assert "pipeline/graph.yaml" in state.policy and "policy/budgets.yaml" in state.policy
    assert [e["event"] for e in rig.events()] == ["enter"]
    assert verify_provenance_chain(rig.log.path).ok


def test_init_rejects_unknown_entry_and_duplicates(rig: Rig, tmp_path: Path) -> None:
    with pytest.raises(RunStateError, match="not an entrypoint"):
        init_run(ROOT, tmp_path / "other", "run-2", "A1")
    with pytest.raises(RunStateError, match="already exists"):
        init_run(ROOT, rig.run_dir, RUN_ID, "START")


def test_invalid_state_is_never_written(rig: Rig) -> None:
    store = RunStore(rig.run_dir)
    state = store.load()
    state.status = Status.PAUSED  # a pause without a reason is invalid
    with pytest.raises(RunStateError, match="refusing to write"):
        store.save(state)
    assert store.load().status is Status.RUNNING


def test_corrupt_state_file_refuses_to_load(rig: Rig) -> None:
    (rig.run_dir / "state.json").write_text('{"version": 1,')
    with pytest.raises(RunStateError, match="unreadable"):
        rig.open()


def test_single_runner_per_run(rig: Rig) -> None:
    first = rig.open()
    with pytest.raises(RunLocked):
        rig.open()
    first.close()
    rig.open().close()


# ── Walking the graph ────────────────────────────────────────────────────────


def test_walks_grounding_into_model_stages(rig: Rig) -> None:
    runner = rig.open(GROUNDING)
    result = runner.run()
    assert (result.outcome, result.node) == (Outcome.BLOCKED, "A4")  # A4 has no handler yet
    assert "no handler registered for A4" in result.detail
    assert [e["state"] for e in rig.events() if e["event"] == "enter"] == [
        "START", "G", "A1", "A2", "A2J", "A3", "A4",
    ]  # fmt: skip
    assert stored(rig).steps == 6
    assert verify_provenance_chain(rig.log.path).ok
    assert {g.operation_id for g in rig.adapters["claude_cli_default"].grants} >= {"proposal_a"}


def test_a_stage_with_several_edges_needs_a_handler(rig: Rig) -> None:
    runner = rig.at("A4", GROUNDING)  # A4 can go to A1, A3, A3R or H1; A2 has one edge, no handler
    result = runner.step()
    assert (result.outcome, result.node) == (Outcome.BLOCKED, "A4")
    assert "no handler registered for A4" in result.detail


def test_blocked_step_changes_nothing(rig: Rig) -> None:
    runner = rig.at("B1", {})  # explicit empty handler table: nothing may decide
    before = (rig.run_dir / "state.json").read_text()
    events_before = rig.log.path.read_text()
    result = runner.step()
    assert result.outcome is Outcome.BLOCKED and "no handler registered for B1" in result.detail
    assert (rig.run_dir / "state.json").read_text() == before
    assert rig.log.path.read_text() == events_before


def test_unknown_guard_from_handler_is_blocked(rig: Rig) -> None:
    runner = rig.open({"START": lambda ctx: "approve_everything"})
    result = runner.step()
    assert result.outcome is Outcome.BLOCKED and "no edge with guard" in result.detail
    assert stored(rig).current == "START"


def test_failing_handler_blocks_and_can_retry(rig: Rig) -> None:
    calls = {"n": 0}

    def flaky(ctx: Any) -> str:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("index unavailable")
        return "task_class_valid"

    runner = rig.open({"START": flaky})
    result = runner.step()
    assert result.outcome is Outcome.BLOCKED and "handler_error: RuntimeError" in result.detail
    assert stored(rig).status is Status.RUNNING  # nothing spent: no human needed
    assert runner.step().outcome is Outcome.ADVANCED


def test_human_state_awaits_a_human(rig: Rig) -> None:
    runner = rig.at("H1")
    assert runner.step().outcome is Outcome.AWAITING_HUMAN
    assert stored(rig).status is Status.AWAITING_HUMAN
    assert runner.step().outcome is Outcome.AWAITING_HUMAN


def test_terminal_state_finishes_and_logs_terminal(rig: Rig) -> None:
    runner = rig.at("T-REJECTED")
    assert runner.step().outcome is Outcome.FINISHED
    assert rig.events()[-1]["event"] == "terminal"
    assert stored(rig).status is Status.FINISHED
    assert runner.step().outcome is Outcome.FINISHED


def test_budgeted_non_model_edge_is_charged(rig: Rig) -> None:
    runner = rig.at("X", {"X": lambda ctx: "infra_rerun_e2"})
    assert runner.step().outcome is Outcome.ADVANCED
    state = stored(rig)
    assert (state.current, state.ledger["consumed"]) == ("E2", {"infra": 1})


def test_max_steps_bounds_a_run(rig: Rig) -> None:
    runner = rig.open(GROUNDING)
    result = runner.run(max_steps=1)
    assert (result.outcome, result.node) == (Outcome.ADVANCED, "G")


# ── Dispatch gates ───────────────────────────────────────────────────────────


def test_unready_backend_blocks_without_pausing(rig: Rig) -> None:
    rig.unready = ("gemini_3_8_flash_high",)
    runner = rig.at("G", GROUNDING)
    result = runner.step()
    assert result.outcome is Outcome.BLOCKED and "gemini_3_8_flash_high.executable" in result.detail
    state = stored(rig)
    assert (state.status, state.current, state.entering) == (Status.RUNNING, "G", None)
    assert state.ledger == {"consumed": {}, "reserved": {}}
    assert not rig.adapters["claude_cli_default"].grants


def test_models_cannot_choose_guards_without_a_handler(rig: Rig) -> None:
    # A4 (decision) would fan out to H1/B1/A1/A3; with no handler nothing is chosen.
    runner = rig.at("A4")
    assert runner.step().outcome is Outcome.BLOCKED
    assert stored(rig).current == "A4"


# ── Durability of budget and entering marker ─────────────────────────────────


def test_marker_and_reservation_are_durable_before_any_model_call(rig: Rig) -> None:
    seen: list[dict[str, Any]] = []

    def watch() -> None:
        doc = json.loads((rig.run_dir / "state.json").read_text())
        seen.append({"entering": doc["entering"], "ledger": doc["ledger"]})

    rig.adapters["claude_cli_default"].side_effect = watch
    runner = rig.at("A4", {"A4": lambda ctx: "proposal_invalid"})
    assert runner.step().outcome is Outcome.ADVANCED
    # While the adapter was running: marker set, unit already spent on disk.
    assert seen[0] == {
        "entering": {
            "from": "A4",
            "guard": "proposal_invalid",
            "to": "A1",
            "after_seq": 1,
            "consumed_before": {},
        },
        "ledger": {"consumed": {"design": 1}, "reserved": {}},
    }
    final = stored(rig)
    assert (final.current, final.entering, final.steps) == ("A1", None, 1)


def test_reservation_is_written_before_the_commit(rig: Rig) -> None:
    runner = rig.at("A4", {"A4": lambda ctx: "proposal_invalid"})
    ledgers: list[dict[str, dict[str, int]]] = []
    real_save = runner.store.save

    def spy(state: RunState) -> None:
        ledgers.append(copy.deepcopy(state.ledger))
        real_save(state)

    runner.store.save = spy  # type: ignore[method-assign]
    runner.step()
    assert {"consumed": {}, "reserved": {"design": 1}} in ledgers
    assert ledgers.index({"consumed": {}, "reserved": {"design": 1}}) < ledgers.index(
        {"consumed": {"design": 1}, "reserved": {}}
    )


def test_budget_survives_reopening(rig: Rig) -> None:
    runner = rig.at("A4", {"A4": lambda ctx: "proposal_invalid"})
    runner.step()
    runner.close()
    again = rig.open()
    assert again.ledger.consumed == {"design": 1}
    assert again.state.current == "A1"


def test_unpersistable_ledger_blocks_dispatch_and_spends_nothing(rig: Rig) -> None:
    runner = rig.at("A4", {"A4": lambda ctx: "proposal_invalid"})
    real_save = runner.store.save

    def failing(state: RunState) -> None:
        if state.ledger["reserved"] or state.ledger["consumed"]:
            raise OSError("disk full")
        real_save(state)

    runner.store.save = failing  # type: ignore[method-assign]
    result = runner.step()
    assert result.outcome is Outcome.BLOCKED and "cannot persist budget ledger" in result.detail
    assert runner.ledger.consumed == {} and runner.ledger.reserved == {"design": 0}
    assert not rig.adapters["claude_cli_default"].grants
    assert all(e["event"] != "model_call" for e in rig.events())


# ── Pause and resume ─────────────────────────────────────────────────────────


def paused_rig(rig: Rig) -> Runner:
    rig.adapters["claude_cli_default"].status = "quota_exhausted"
    runner = rig.at("A4", {"A4": lambda ctx: "proposal_invalid"})
    assert runner.step().outcome is Outcome.PAUSED
    return runner


def test_quota_exhaustion_pauses_and_keeps_the_spent_budget(rig: Rig) -> None:
    paused_rig(rig)
    state = stored(rig)
    assert state.status is Status.PAUSED and "quota_exhausted" in state.paused_reason
    assert (state.current, state.entering) == ("A4", None)
    assert state.ledger["consumed"] == {"design": 1}
    assert not rig.adapters["gemini_3_8_flash_high"].grants  # no fallback to another provider


def test_paused_run_refuses_to_step_even_after_reopening(rig: Rig) -> None:
    paused_rig(rig).close()
    calls = len(rig.adapters["claude_cli_default"].grants)
    again = rig.open({"A4": lambda ctx: "proposal_invalid"})
    result = again.step()
    assert result.outcome is Outcome.PAUSED and "quota_exhausted" in result.detail
    assert len(rig.adapters["claude_cli_default"].grants) == calls


def test_resume_is_recorded_and_the_edge_is_charged_again(rig: Rig) -> None:
    paused_rig(rig).close()
    store = RunStore(rig.run_dir)
    state = store.load()
    resume(state, rig.log, by="ln", reason="quota reset")
    store.save(state)

    assert state.status is Status.RUNNING and state.resumes[0]["by"] == "ln"
    decisions = [e for e in rig.events() if e["event"] == "decision"]
    assert decisions[-1]["approver_id"] == "unverified:ln"
    assert decisions[-1]["reason_code"] == "human_resume"
    assert state.resumes[0]["identity"] == "self-asserted"
    assert state.resumes[0]["provenance_tip"] == decisions[-1]["event_hash"]

    rig.adapters["claude_cli_default"].status = "ok"
    again = rig.open({"A4": lambda ctx: "proposal_invalid"})
    assert again.step().outcome is Outcome.ADVANCED
    assert again.ledger.consumed == {"design": 2}


def test_resume_needs_a_paused_run_and_a_named_human(rig: Rig) -> None:
    store = RunStore(rig.run_dir)
    state = store.load()
    with pytest.raises(RunStateError, match="not paused"):
        resume(state, rig.log, by="ln", reason="why")
    state.pause("quota")
    with pytest.raises(RunStateError, match="--by and --reason"):
        resume(state, rig.log, by=" ", reason="why")


# ── Crash recovery ───────────────────────────────────────────────────────────


def crash_after_model_call(rig: Rig) -> None:
    """Leave the run as a process killed right after logging a model_call would."""
    runner = rig.at("A4")
    runner.state.entering = {
        "from": "A4",
        "guard": "proposal_invalid",
        "to": "A1",
        "after_seq": rig.log.count(),
        "consumed_before": {},
    }
    runner.state.ledger = {"consumed": {}, "reserved": {"design": 1}}
    runner.store.save(runner.state)
    rig.log.append(
        {
            "state": "A1",
            "event": "model_call",
            "model": "fake",
            "model_version": "1",
            "prompt_hash": "sha256:" + "a" * 64,
            "backend": "claude_cli_default",
            "access_method": "subscription_tool",
            "operation_id": "proposal_a",
            "invocation_id": "inv-crash",
        }
    )
    runner.close()


def test_crash_mid_dispatch_pauses_and_counts_the_unit_as_spent(rig: Rig) -> None:
    crash_after_model_call(rig)
    runner = rig.open()
    state = runner.state
    assert state.status is Status.PAUSED and state.paused_reason == "crash_during_dispatch"
    assert state.ledger == {"consumed": {"design": 1}, "reserved": {}}
    assert state.entering is None and runner.ledger.consumed == {"design": 1}
    assert runner.step().outcome is Outcome.PAUSED


def test_resume_closes_the_unfinished_call_so_the_log_verifies(rig: Rig) -> None:
    crash_after_model_call(rig)
    assert verify_provenance_chain(rig.log.path).unresolved == ["inv-crash"]
    store = RunStore(rig.run_dir)
    runner = rig.open()
    resume(runner.state, rig.log, by="ln", reason="checked the provider dashboard")
    store.save(runner.state)
    runner.close()

    report_after = verify_provenance_chain(rig.log.path)
    assert report_after.ok, report_after
    closing = [e for e in rig.events() if e.get("invocation_id") == "inv-crash"][-1]
    assert (closing["event"], closing["reason_code"]) == ("exception", "human_reconciled")
    assert rig.open().state.status is Status.RUNNING


def test_unresolved_call_alone_pauses_the_run(rig: Rig) -> None:
    crash_after_model_call(rig)
    store = RunStore(rig.run_dir)
    state = store.load()
    state.ledger, state.entering = {"consumed": {"design": 1}, "reserved": {}}, None
    store.save(state)
    reopened = rig.open()
    assert reopened.state.paused_reason == "unresolved_invocation"


def test_tampered_provenance_refuses_to_open(rig: Rig) -> None:
    lines = rig.log.path.read_text().splitlines()
    record = json.loads(lines[0])
    record["state"] = "A1"
    rig.log.path.write_text(json.dumps(record) + "\n")
    with pytest.raises(RunStateError, match="provenance chain does not verify"):
        rig.open()
    rig.open  # noqa: B018 - lock must have been released by the failed open
    RunStore(rig.run_dir).acquire()


# ── Policy drift ─────────────────────────────────────────────────────────────


def policy_copy(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    for name in ("pipeline", "policy"):
        shutil.copytree(ROOT / name, root / name)
    return root


@pytest.mark.parametrize(
    ("relative", "mutate"),
    [
        ("policy/budgets.yaml", lambda t: t.replace("max_retries: 2", "max_retries: 9")),
        ("pipeline/graph.yaml", lambda t: t + "\n# edited\n"),
    ],
    ids=["budget-raised", "graph-edited"],
)
def test_changed_policy_refuses_to_continue(
    tmp_path: Path, relative: str, mutate: Callable[[str], str]
) -> None:
    root = policy_copy(tmp_path)
    rig = Rig(tmp_path, root)
    rig.open().close()
    path = root / relative
    path.write_text(mutate(path.read_text()))
    with pytest.raises(PolicyDrift, match=relative):
        rig.open()


# ── Review fixes: commit record, guarded saves, policy snapshot ──────────────


def test_dispatcher_gets_the_policy_that_was_checked(rig: Rig) -> None:
    runner = rig.open()
    assert rig.policy_seen.digests == runner.state.policy
    assert runner.registry is rig.policy_seen.registry and runner.graph is rig.policy_seen.graph


def test_save_failure_after_a_finished_stage_pauses_then_recovers_forward(rig: Rig) -> None:
    runner = rig.at("A4", {"A4": lambda ctx: "proposal_invalid"})
    real_save = runner.store.save

    def fail_final(state: RunState) -> None:
        if state.current == "A1":  # only the post-commit save fails
            raise OSError("fsync failed")
        real_save(state)

    runner.store.save = fail_final  # type: ignore[method-assign]
    result = runner.step()
    assert result.outcome is Outcome.PAUSED and "state_save_failed" in result.detail
    calls = len(rig.adapters["claude_cli_default"].grants)
    runner.close()

    reopened = rig.open()  # disk: marker still set, enter event logged
    state = reopened.state
    assert (state.status, state.current, state.entering) == (Status.RUNNING, "A1", None)
    assert state.ledger["consumed"] == {"design": 1}  # charged once, not twice
    assert len(rig.adapters["claude_cli_default"].grants) == calls  # nothing re-run


def test_crash_after_enter_event_recovers_forward(rig: Rig) -> None:
    runner = rig.at("G")
    runner.state.entering = {
        "from": "G",
        "guard": "always",
        "to": "A1",
        "after_seq": rig.log.count(),
        "consumed_before": {},
    }
    runner.store.save(runner.state)
    rig.log.append({"state": "A1", "event": "enter"})
    runner.close()
    state = rig.open().state
    assert (state.status, state.current, state.steps) == (Status.RUNNING, "A1", 1)


def test_older_enter_for_the_same_target_does_not_count(rig: Rig) -> None:
    rig.log.append({"state": "A1", "event": "enter"})  # an earlier visit
    runner = rig.at("G")
    runner.state.entering = {
        "from": "G",
        "guard": "always",
        "to": "A1",
        "after_seq": rig.log.count(),
        "consumed_before": {},
    }
    runner.state.ledger = {"consumed": {}, "reserved": {"design": 1}}  # work had started
    runner.store.save(runner.state)
    runner.close()
    state = rig.open().state
    assert (state.status, state.paused_reason) == (Status.PAUSED, "crash_during_dispatch")


def test_unwritable_enter_event_pauses_without_advancing(rig: Rig) -> None:
    runner = rig.at("X", {"X": lambda ctx: "infra_rerun_e2"})
    real_append = runner.log.append

    def refuse_enter(event: Mapping[str, Any]) -> dict[str, Any]:
        if event.get("event") == "enter":
            raise OSError("log volume read-only")
        return real_append(event)

    runner.log.append = refuse_enter  # type: ignore[method-assign]
    result = runner.step()
    assert result.outcome is Outcome.PAUSED and "provenance_unavailable" in result.detail
    assert stored(rig).current == "X"


def test_terminal_event_is_not_duplicated_after_a_failed_save(rig: Rig) -> None:
    runner = rig.at("T-REJECTED")
    real_save = runner.store.save

    def fail_finished(state: RunState) -> None:
        if state.status is Status.FINISHED:
            raise OSError("disk full")
        real_save(state)

    runner.store.save = fail_finished  # type: ignore[method-assign]
    assert runner.step().outcome is Outcome.PAUSED
    runner.close()

    store = RunStore(rig.run_dir)
    state = store.load()
    resume(state, rig.log, by="ln", reason="disk replaced")  # appends a decision event
    store.save(state)
    assert rig.events()[-1]["event"] == "decision"
    assert rig.open().step().outcome is Outcome.FINISHED
    assert [e["event"] for e in rig.events()].count("terminal") == 1


# ── Second review round ──────────────────────────────────────────────────────


def test_spent_budget_takes_the_spent_route(rig: Rig) -> None:
    runner = rig.at("A4", {"A4": lambda ctx: "proposal_invalid"})
    runner.ledger.consumed = {"design": 2}  # max_retries for design is 2
    runner._save()
    result = runner.step()
    assert result.outcome is Outcome.ADVANCED
    assert result.detail == "budget design spent: A4 --design_spent--> H1"
    state = stored(rig)
    assert (state.current, state.entering, state.status) == ("H1", None, Status.RUNNING)
    assert state.ledger["consumed"] == {"design": 2}  # nothing more spent
    assert not rig.adapters["claude_cli_default"].grants
    assert runner.step().outcome is Outcome.AWAITING_HUMAN


def test_spent_budget_on_a_non_model_edge_takes_its_route(rig: Rig) -> None:
    runner = rig.at("X", {"X": lambda ctx: "infra_rerun_e2"})
    runner.ledger.consumed = {"infra": 2}
    runner._save()
    result = runner.step()
    assert (result.outcome, result.node) == (Outcome.ADVANCED, "H1")
    assert result.detail == "budget infra spent: X --unknown_or_spent--> H1"


def test_spent_budget_without_a_route_pauses_with_a_typed_cause(rig: Rig) -> None:
    runner = rig.at("A4", {"A4": lambda ctx: "proposal_invalid"})
    runner.budgets["design"] = {**runner.budgets["design"], "on_exhausted": "B2"}  # no A4->B2
    runner.ledger.consumed = {"design": 2}
    runner._save()
    result = runner.step()
    assert result.outcome is Outcome.PAUSED and "no spent route from A4" in result.detail
    assert stored(rig).pause_cause is PauseCause.BUDGET_EXHAUSTED


def test_pauses_record_their_cause(rig: Rig) -> None:
    rig.adapters["claude_cli_default"].status = "quota_exhausted"
    rig.at("A4", {"A4": lambda ctx: "proposal_invalid"}).step()
    assert stored(rig).pause_cause is PauseCause.DISPATCH_PAUSED


def test_stale_marker_is_cleared_without_pausing(rig: Rig) -> None:
    runner = rig.at("G")
    runner.state.entering = {
        "from": "G",
        "guard": "always",
        "to": "A1",
        "after_seq": rig.log.count(),
        "consumed_before": {},
    }  # e.g. a blocked step whose cleanup save failed: nothing ran
    runner.store.save(runner.state)
    runner.close()
    state = rig.open().state
    assert (state.status, state.current, state.entering) == (Status.RUNNING, "G", None)


def test_policy_edit_mid_session_blocks_the_next_step(tmp_path: Path) -> None:
    root = policy_copy(tmp_path)
    rig = Rig(tmp_path, root)
    runner = rig.open(GROUNDING)
    assert runner.step().outcome is Outcome.ADVANCED
    weights = root / "policy" / "weights.yaml"
    weights.write_text(weights.read_text() + "\n# edited mid-run\n")
    result = runner.step()
    assert result.outcome is Outcome.BLOCKED and "changed during this session" in result.detail
    assert stored(rig).current == "G"


@pytest.mark.parametrize(
    ("relative", "content"),
    [
        ("policy/budgets.yaml", "budgets: [unclosed"),
        ("policy/budgets.yaml", "version: 1\nbudgets: 5\n"),
        ("pipeline/graph.yaml", "states: 7\n"),
    ],
    ids=["yaml-syntax", "budgets-wrong-shape", "graph-wrong-shape"],
)
def test_malformed_policy_is_a_clean_error(tmp_path: Path, relative: str, content: str) -> None:
    root = policy_copy(tmp_path)
    (root / relative).write_text(content)
    with pytest.raises(RunStateError, match="cannot read graph or policy|malformed"):
        if relative.startswith("pipeline"):
            from agentic_pipeline.run_state import load_policy

            load_policy(root)
        else:
            Rig(tmp_path, root).open()  # shape errors surface when the ledger is built


def test_error_before_anything_is_spent_blocks_instead_of_pausing(rig: Rig) -> None:
    runner = rig.at("G", GROUNDING)

    def broken_preflight() -> Any:
        raise OSError("probe failed")

    runner.dispatcher.preflight = broken_preflight
    result = runner.step()
    assert result.outcome is Outcome.BLOCKED and "unexpected_error: OSError" in result.detail
    state = stored(rig)
    assert (state.status, state.current, state.entering) == (Status.RUNNING, "G", None)


def test_error_after_spending_still_pauses(rig: Rig) -> None:
    def explode() -> None:
        raise ValueError("adapter bug after the call was logged")

    rig.adapters["claude_cli_default"].side_effect = explode
    runner = rig.at("A4", {"A4": lambda ctx: "proposal_invalid"})
    result = runner.step()
    assert result.outcome is Outcome.PAUSED
    assert stored(rig).ledger["consumed"] == {"design": 1}
