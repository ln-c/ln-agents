"""Tests for `agentic_pipeline run ...` against real policy and a temporary run directory."""

from __future__ import annotations

from pathlib import Path

import pytest

from agentic_pipeline import run_cli
from agentic_pipeline.cli import main
from agentic_pipeline.run_state import RunStore, Status


def run(*args: str, runs_dir: Path) -> int:
    return main(["run", args[0], *args[1:], "--runs-dir", str(runs_dir)])


def test_init_status_and_blocked_step(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert run("init", "demo", runs_dir=tmp_path) == 0
    assert run("status", "demo", runs_dir=tmp_path) == 0
    out = capsys.readouterr().out
    assert "created at START" in out and "status:   running" in out

    assert run("step", "demo", runs_dir=tmp_path) == 1  # blocked: no START handler yet
    assert "blocked: at START no handler registered for START" in capsys.readouterr().out
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
    state.entering = {"from": "START", "guard": "task_class_valid", "to": "G1", "after_seq": 1}
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
    monkeypatch.setattr(adapters.PiQwenAdapter, "from_registry", staticmethod(no_ollama))
    lazy = run_cli.LazyAdapters({})
    assert list(lazy) == []
    assert lazy.unavailable("claude_pro") == "LookupError: credentials.json not found"
    assert lazy.unavailable("qwen_local") == "OSError: connection refused"
    assert lazy.unavailable("google_ai_pro") is None


def test_resume_refuses_a_crash_it_just_discovered(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], human: None
) -> None:
    assert run("init", "demo", runs_dir=tmp_path) == 0
    store = RunStore(tmp_path / "demo")
    state = store.load()  # on disk: running, but a dispatch was in flight
    state.entering = {"from": "START", "guard": "task_class_valid", "to": "G1", "after_seq": 1}
    store.save(state)

    assert run("resume", "demo", "--by", "ln", "--reason", "x", runs_dir=tmp_path) == 1
    assert "recovery has now paused it (crash_during_dispatch)" in capsys.readouterr().err
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
    assert list(lazy) == []
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
