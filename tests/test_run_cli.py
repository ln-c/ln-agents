"""Tests for `agentic_pipeline run ...` against real policy and a temporary run directory."""

from __future__ import annotations

import fcntl
import json
import os
import shutil
import threading
from pathlib import Path

import pytest

from agentic_pipeline import run_cli
from agentic_pipeline.cli import main
from agentic_pipeline.run_state import PauseCause, RunStore, Status


def run(*args: str, runs_dir: Path) -> int:
    return main(["run", args[0], *args[1:], "--runs-dir", str(runs_dir)])


def test_init_status_and_blocked_step(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert run("init", "demo", runs_dir=tmp_path) == 0
    assert run("status", "demo", runs_dir=tmp_path) == 0
    out = capsys.readouterr().out
    assert "created at START" in out and "status:   running" in out

    assert run("step", "demo", runs_dir=tmp_path) == 1  # blocked: no entry spec was supplied
    assert "blocked: at START" in capsys.readouterr().out
    assert RunStore(tmp_path / "demo").load().steps == 0


def test_duplicate_and_bad_run_ids_are_errors(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run("init", "demo", runs_dir=tmp_path) == 0
    assert run("init", "demo", runs_dir=tmp_path) == 1
    assert "run already exists" in capsys.readouterr().err
    assert run("init", "../escape", runs_dir=tmp_path) == 1
    assert not (tmp_path.parent / "escape").exists()


def test_status_of_unknown_run_is_an_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run("status", "nope", runs_dir=tmp_path) == 1
    assert "no run state" in capsys.readouterr().err


@pytest.fixture
def human(monkeypatch: pytest.MonkeyPatch) -> None:
    """Stand in for the interactive confirmation a real person would type."""
    monkeypatch.setattr(run_cli, "_confirm_human", lambda run_id: True)


def test_paused_run_exits_2_until_a_human_resumes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], human: None
) -> None:
    assert run("init", "demo", runs_dir=tmp_path) == 0
    store = RunStore(tmp_path / "demo")
    state = store.load()
    state.pause("quota_exhausted")
    store.save(state)

    assert run("step", "demo", runs_dir=tmp_path) == 2
    assert "paused: at START quota_exhausted" in capsys.readouterr().out

    assert run("resume", "demo", "--by", "ln", "--reason", "quota reset", runs_dir=tmp_path) == 0
    resumed = store.load()
    assert resumed.status is Status.RUNNING and resumed.resumes[0]["reason"] == "quota reset"
    assert run("step", "demo", runs_dir=tmp_path) == 1  # running again, still no handler


def test_resume_of_a_running_run_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], human: None
) -> None:
    assert run("init", "demo", runs_dir=tmp_path) == 0
    assert run("resume", "demo", "--by", "ln", "--reason", "x", runs_dir=tmp_path) == 1
    assert "not paused" in capsys.readouterr().err


def test_resume_requires_who_and_why(tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        run("resume", "demo", runs_dir=tmp_path)


def test_resume_refuses_without_an_interactive_terminal(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run("init", "demo", runs_dir=tmp_path) == 0
    store = RunStore(tmp_path / "demo")
    state = store.load()
    state.pause("quota_exhausted")
    store.save(state)
    # Under pytest stdin is not a TTY, exactly like a model's shell tool.
    assert run("resume", "demo", "--by", "agent", "--reason", "x", runs_dir=tmp_path) == 1
    assert "interactive terminal" in capsys.readouterr().err
    assert store.load().status is Status.PAUSED


def test_resume_needs_the_run_id_retyped(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run("init", "demo", runs_dir=tmp_path) == 0
    store = RunStore(tmp_path / "demo")
    state = store.load()
    state.pause("quota_exhausted")
    store.save(state)
    monkeypatch.setattr(run_cli.sys.stdin, "isatty", lambda: True, raising=False)
    monkeypatch.setattr(run_cli.sys.stdout, "isatty", lambda: True, raising=False)
    monkeypatch.setattr("builtins.input", lambda prompt: "wrong")
    assert run("resume", "demo", "--by", "ln", "--reason", "x", runs_dir=tmp_path) == 1
    assert store.load().status is Status.PAUSED


def test_status_previews_recovery(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert run("init", "demo", runs_dir=tmp_path) == 0
    store = RunStore(tmp_path / "demo")
    state = store.load()
    state.entering = {
        "from": "START",
        "guard": "task_class_valid",
        "to": "G1",
        "after_seq": 1,
        "consumed_before": {},
    }
    state.ledger["reserved"] = {"design": 1}
    store.save(state)
    capsys.readouterr()
    assert run("status", "demo", runs_dir=tmp_path) == 0
    out = capsys.readouterr().out
    assert "on open:  paused (crash_during_dispatch) at START" in out
    assert "recovery: reserved units {'design': 1} counted as spent" in out
    assert store.load().status is Status.RUNNING  # status itself changed nothing


def test_adapter_build_failures_are_explained(monkeypatch: pytest.MonkeyPatch) -> None:
    from agentic_pipeline import adapters

    def no_creds(registry: object) -> object:
        raise LookupError("credentials.json not found")

    def no_ollama(registry: object) -> object:
        raise OSError("connection refused")

    monkeypatch.setattr(adapters.ClaudeAdapter, "from_registry", staticmethod(no_creds))

    def no_token(registry: object) -> object:
        raise LookupError("antigravity-oauth-token not found")

    monkeypatch.setattr(adapters.PiQwenAdapter, "from_registry", staticmethod(no_ollama))
    monkeypatch.setattr(adapters.AgyAdapter, "from_registry", staticmethod(no_token))
    lazy = run_cli.LazyAdapters({})
    assert "claude_pro" not in lazy and "qwen_local" not in lazy and "google_ai_pro" not in lazy
    assert lazy.unavailable("claude_pro") == "LookupError: credentials.json not found"
    assert lazy.unavailable("qwen_local") == "OSError: connection refused"
    assert lazy.unavailable("google_ai_pro") == "LookupError: antigravity-oauth-token not found"
    assert lazy.unavailable("clef_flash_free") is None  # no adapter exists for it at all


def test_resume_refuses_a_crash_it_just_discovered(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], human: None
) -> None:
    assert run("init", "demo", runs_dir=tmp_path) == 0
    store = RunStore(tmp_path / "demo")
    state = store.load()  # on disk: running, but a dispatch was in flight and reserved budget
    state.entering = {
        "from": "START",
        "guard": "task_class_valid",
        "to": "G1",
        "after_seq": 1,
        "consumed_before": {},
    }
    state.ledger["reserved"] = {"design": 1}
    store.save(state)

    assert run("resume", "demo", "--by", "ln", "--reason", "x", runs_dir=tmp_path) == 1
    assert (
        "recovery changed this run; now paused (crash_during_dispatch)" in capsys.readouterr().err
    )
    assert store.load().status is Status.PAUSED  # recovered state was saved, not resumed

    assert run("resume", "demo", "--by", "ln", "--reason", "reviewed", runs_dir=tmp_path) == 0
    assert store.load().status is Status.RUNNING


def test_resume_lists_unresolved_calls_before_confirming(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    from agentic_pipeline.provenance import JsonlProvenanceLog

    assert run("init", "demo", runs_dir=tmp_path) == 0
    JsonlProvenanceLog(tmp_path / "demo" / "provenance", "demo").append(
        {
            "state": "A1",
            "event": "model_call",
            "model": "m",
            "model_version": "1",
            "prompt_hash": "sha256:" + "a" * 64,
            "backend": "claude_pro",
            "access_method": "subscription_tool",
            "operation_id": "proposal_a",
            "invocation_id": "inv-9",
        }
    )
    store = RunStore(tmp_path / "demo")
    state = store.load()
    state.pause("unresolved_invocation")
    store.save(state)

    shown: list[str] = []

    def confirm(run_id: str) -> bool:
        shown.append(capsys.readouterr().out)
        return False

    monkeypatch.setattr(run_cli, "_confirm_human", confirm)
    assert run("resume", "demo", "--by", "ln", "--reason", "x", runs_dir=tmp_path) == 1
    assert "unresolved model call inv-9 (A1.proposal_a) will be marked human_reconciled" in shown[0]
    assert store.load().status is Status.PAUSED


def test_confirmation_treats_end_of_input_as_no(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(run_cli.sys.stdin, "isatty", lambda: True, raising=False)
    monkeypatch.setattr(run_cli.sys.stdout, "isatty", lambda: True, raising=False)

    def eof(prompt: str) -> str:
        raise EOFError

    monkeypatch.setattr("builtins.input", eof)
    assert run_cli._confirm_human("demo") is False


def test_unexpected_adapter_errors_are_explained(monkeypatch: pytest.MonkeyPatch) -> None:
    from agentic_pipeline import adapters

    def broken(registry: object) -> object:
        raise TypeError("missing field 'readiness'")

    monkeypatch.setattr(adapters.ClaudeAdapter, "from_registry", staticmethod(broken))
    monkeypatch.setattr(adapters.PiQwenAdapter, "from_registry", staticmethod(broken))
    lazy = run_cli.LazyAdapters({})
    assert "claude_pro" not in lazy and "qwen_local" not in lazy
    assert lazy.unavailable("claude_pro") == "TypeError: missing field 'readiness'"


def test_dispatcher_readiness_uses_the_frozen_registry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from agentic_pipeline.provenance import JsonlProvenanceLog
    from agentic_pipeline.run_state import load_policy
    from agentic_pipeline.runner import ChosenGuard
    from agentic_pipeline.transition import BudgetLedger

    seen: dict[str, object] = {}

    def fake_preflight(root: Path, host: object = None, registry: object = None) -> object:
        seen["registry"] = registry
        return None

    monkeypatch.setattr(run_cli, "run_preflight", fake_preflight)
    policy = load_policy(run_cli.REPO_ROOT)
    dispatcher = run_cli._dispatcher_factory(run_cli.REPO_ROOT, tmp_path)(
        policy, JsonlProvenanceLog(tmp_path, "demo"), BudgetLedger({}), ChosenGuard()
    )
    dispatcher.preflight()
    assert seen["registry"] is policy.registry


# ── Third review round ───────────────────────────────────────────────────────


def _paused(tmp_path: Path, reason: str, cause: PauseCause = PauseCause.OTHER) -> RunStore:
    assert run("init", "demo", runs_dir=tmp_path) == 0
    store = RunStore(tmp_path / "demo")
    state = store.load()
    state.pause(reason, cause)
    store.save(state)
    return store


def test_resume_refuses_when_recovery_changes_a_paused_run(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], human: None
) -> None:
    store = _paused(tmp_path, "provenance_unavailable: disk")
    state = store.load()
    state.ledger["reserved"] = {"design": 1}  # left behind by an interrupted step
    store.save(state)

    assert run("resume", "demo", "--by", "ln", "--reason", "x", runs_dir=tmp_path) == 1
    err = capsys.readouterr().err
    assert "recovery changed this run" in err and "reserved units {'design': 1}" in err
    assert store.load().ledger == {"consumed": {"design": 1}, "reserved": {}}
    assert store.load().status is Status.PAUSED

    assert run("resume", "demo", "--by", "ln", "--reason", "reviewed", runs_dir=tmp_path) == 0


def test_resume_refuses_an_exhausted_budget(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], human: None
) -> None:
    store = _paused(tmp_path, "budget gone", PauseCause.BUDGET_EXHAUSTED)
    assert run("resume", "demo", "--by", "ln", "--reason", "x", runs_dir=tmp_path) == 1
    assert "would retry the same exhausted edge" in capsys.readouterr().err
    assert store.load().status is Status.PAUSED


def test_resume_decides_by_cause_not_reason_text(tmp_path: Path, human: None) -> None:
    store = _paused(tmp_path, "budget_exhausted: looks like it, but is not")
    assert run("resume", "demo", "--by", "ln", "--reason", "x", runs_dir=tmp_path) == 0
    assert store.load().status is Status.RUNNING


def test_copied_run_directory_is_refused(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run("init", "demo", runs_dir=tmp_path) == 0
    shutil.copytree(tmp_path / "demo", tmp_path / "copy")
    assert run("status", "copy", runs_dir=tmp_path) == 1
    assert "belongs to run 'demo', not 'copy'" in capsys.readouterr().err


def test_acquire_waits_out_a_momentary_holder(tmp_path: Path) -> None:
    assert run("init", "demo", runs_dir=tmp_path) == 0
    store = RunStore(tmp_path / "demo")
    fd = os.open(tmp_path / "demo" / "state.lock", os.O_RDWR)
    fcntl.flock(fd, fcntl.LOCK_EX)
    threading.Timer(0.2, os.close, args=(fd,)).start()  # e.g. `run status` probing
    store.acquire()  # waits instead of failing
    store.release()


def test_status_does_not_report_a_live_runner_as_a_crash(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    assert run("init", "demo", runs_dir=tmp_path) == 0
    store = RunStore(tmp_path / "demo")
    state = store.load()
    state.entering = {
        "from": "START",
        "guard": "task_class_valid",
        "to": "G1",
        "after_seq": 1,
        "consumed_before": {},
    }
    state.ledger["reserved"] = {"design": 1}  # the in-flight step had reserved budget
    store.save(state)
    live = RunStore(tmp_path / "demo")
    live.acquire()  # another process is mid-step
    try:
        capsys.readouterr()
        assert run("status", "demo", runs_dir=tmp_path) == 0
        out = capsys.readouterr().out
        assert "runner:   active" in out and "crash_during_dispatch" not in out
    finally:
        live.release()
    assert run("status", "demo", runs_dir=tmp_path) == 0
    assert "on open:  paused (crash_during_dispatch)" in capsys.readouterr().out


def test_only_the_needed_adapter_is_built(monkeypatch: pytest.MonkeyPatch) -> None:
    built: list[str] = []

    def factory(name: str) -> object:
        def make(registry: object) -> object:
            built.append(name)
            return object()

        return make

    monkeypatch.setattr(
        run_cli,
        "_adapter_factories",
        lambda: {"qwen_local": factory("qwen_local"), "claude_pro": factory("claude_pro")},
    )
    lazy = run_cli.LazyAdapters({})
    assert "qwen_local" in lazy and lazy["qwen_local"] is not None
    assert built == ["qwen_local"]  # the Claude adapter (and its auth probe) never ran


@pytest.mark.parametrize("value", ["0", "-3"])
def test_max_steps_must_be_positive(tmp_path: Path, value: str) -> None:
    with pytest.raises(SystemExit):
        run("step", "demo", "--max-steps", value, runs_dir=tmp_path)


def test_init_copies_a_valid_entry_spec_and_rejects_a_bad_one(tmp_path: Path) -> None:
    from test_grounding import SPEC  # the grounding agent's example payload

    good = tmp_path / "good.json"
    good.write_text(json.dumps(SPEC))
    assert run("init", "demo", "--entry-spec", str(good), runs_dir=tmp_path) == 0
    assert (tmp_path / "demo" / "inputs" / "entry_spec.json").read_text() == good.read_text()

    bad = tmp_path / "bad.json"
    bad.write_text("{}")
    assert run("init", "other", "--entry-spec", str(bad), runs_dir=tmp_path) == 1
    assert not (tmp_path / "other").exists()
