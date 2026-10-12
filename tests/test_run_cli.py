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
