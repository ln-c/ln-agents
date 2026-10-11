"""Preflight runs real host probes and must leave the filesystem untouched."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from agentic_pipeline.readiness import Host, run_preflight

ROOT = Path(__file__).resolve().parents[1]


def _snapshot(*roots: Path) -> dict[Path, tuple[int, int]]:
    return {
        p: (p.stat().st_size, p.stat().st_mtime_ns)
        for root in roots
        for p in sorted(root.rglob("*"))
    }


def test_preflight_creates_and_modifies_no_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    home, cwd = tmp_path / "home", tmp_path / "cwd"
    home.mkdir()
    cwd.mkdir()
    monkeypatch.chdir(cwd)
    host = Host(environ={"PATH": os.environ.get("PATH", "")}, home=home)

    repo_before = _snapshot(ROOT / "pipeline", ROOT / "policy", ROOT / "schemas")
    report = run_preflight(ROOT, host)

    assert not report.static_errors
    assert not any(home.iterdir()), list(home.rglob("*"))
    assert not any(cwd.iterdir()), list(cwd.rglob("*"))
    assert _snapshot(ROOT / "pipeline", ROOT / "policy", ROOT / "schemas") == repo_before
