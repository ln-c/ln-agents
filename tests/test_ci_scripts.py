"""scripts/ci_mcdc.py and scripts/ci_mutation.py: the tooling the CI checks run."""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

ROOT = Path(__file__).resolve().parents[1]


def load(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


mcdc = load("ci_mcdc")
mutation = load("ci_mutation")

SOURCE = """def ok(a, b, c):
    if (a and b) or c:
        return True
    return False
"""
FULL = """import unittest

from pkg.mod import ok


class T(unittest.TestCase):
    def test_all(self):
        for a, b, c in [(True, True, False), (False, True, False), (True, False, False),
                        (False, False, True), (False, False, False), (True, True, True)]:
            ok(a, b, c)
"""
WEAK = """import unittest

from pkg.mod import ok


class T(unittest.TestCase):
    def test_one(self):
        self.assertTrue(ok(True, True, False))
"""
PYTEST_STYLE = "from pkg.mod import ok\n\n\ndef test_it():\n    assert ok(True, True, False)\n"


def project(tmp_path: Path, tests: dict[str, str]) -> Path:
    (tmp_path / "src" / "pkg").mkdir(parents=True)
    (tmp_path / "src" / "pkg" / "__init__.py").write_text("")
    (tmp_path / "src" / "pkg" / "mod.py").write_text(SOURCE)
    (tmp_path / "tests").mkdir()
    for name, text in tests.items():
        (tmp_path / "tests" / name).write_text(text)
    return tmp_path


def run_mcdc(cwd: Path, *files: str) -> str:
    done = subprocess.run(  # noqa: S603
        [
            sys.executable,
            str(ROOT / "scripts" / "ci_mcdc.py"),
            "--tests",
            "tests",
            "--files",
            *files,
        ],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
        env={"PATH": "/usr/bin", "PYTHONPATH": str(cwd / "src")},
    )
    assert done.returncode == 0, done.stderr
    return done.stdout


pymcdc = pytest.importorskip("pymcdc")


def test_full_mcdc_is_reported_as_100(tmp_path: Path) -> None:
    out = run_mcdc(project(tmp_path, {"test_full.py": FULL}), "src/pkg/mod.py")
    assert "MC/DC 100.0%" in out


def test_a_weak_suite_reports_the_gap(tmp_path: Path) -> None:
    out = run_mcdc(project(tmp_path, {"test_weak.py": WEAK}), "src/pkg/mod.py")
    percent = float(out.split("MC/DC ")[1].split("%")[0])
    assert 0 < percent < 100


def test_pytest_style_tests_do_not_count(tmp_path: Path) -> None:
    out = run_mcdc(project(tmp_path, {"test_py.py": PYTEST_STYLE}), "src/pkg/mod.py")
    assert "MC/DC 0.0%" in out  # decisions exist and no unittest test exercises them


def test_only_files_that_define_a_test_case_class_are_unittest_files(tmp_path: Path) -> None:
    (tmp_path / "a.py").write_text("import unittest\nclass T(unittest.TestCase):\n    pass\n")
    (tmp_path / "b.py").write_text("from unittest import TestCase\nclass T(TestCase):\n    pass\n")
    (tmp_path / "c.py").write_text('X = "unittest.TestCase"\ndef test_x():\n    pass\n')
    (tmp_path / "d.py").write_text("def broken(:\n")
    names = [p.name for p in mcdc.unittest_files(tmp_path)]
    assert names == ["a.py", "b.py"]


def test_no_tests_at_all_scores_zero_for_a_file_with_decisions(tmp_path: Path) -> None:
    assert "MC/DC 0.0%" in run_mcdc(project(tmp_path, {}), "src/pkg/mod.py")


def test_files_without_decisions_tests_and_non_python_files_are_fully_covered(
    tmp_path: Path,
) -> None:
    root = project(tmp_path, {"test_full.py": FULL})
    (root / "src" / "pkg" / "plain.py").write_text("def f(x):\n    return x + 1\n")
    out = run_mcdc(root, "src/pkg/plain.py", "tests/test_full.py", "docs/note.md")
    assert "MC/DC 100.0%" in out and "0 files" not in out.splitlines()[-1].split("(")[0]


def test_a_patch_with_no_python_files_is_fully_covered(tmp_path: Path) -> None:
    assert "MC/DC 100.0% (0 of 0 requirements, 0 files)" in run_mcdc(
        project(tmp_path, {}), "docs/note.md"
    )


def test_a_pymcdc_failure_is_an_error_not_a_pass(tmp_path: Path) -> None:
    root = project(tmp_path, {})
    (root / "src" / "pkg" / "broken.py").write_text("def f(:\n")
    done = subprocess.run(  # noqa: S603
        [
            sys.executable,
            str(ROOT / "scripts" / "ci_mcdc.py"),
            "--tests",
            "tests",
            "--files",
            "src/pkg/broken.py",
        ],
        cwd=root,
        capture_output=True,
        text=True,
        check=False,
        env={"PATH": "/usr/bin", "PYTHONPATH": str(root / "src")},
    )
    assert done.returncode == 2 and "pymcdc failed" in done.stderr


def test_the_mutation_score_counts_only_the_mutants_that_were_run() -> None:
    stats = {"killed": 58, "survived": 12, "total": 10491, "no_tests": 0, "timeout": 0}
    assert mutation.score(stats) == pytest.approx(82.857, abs=0.01)
    assert mutation.score({"killed": 0, "survived": 0, "total": 10491}) == 100.0
    assert mutation.score({"killed": 1, "survived": 1, "timeout": 1, "suspicious": 1}) == 25.0


def test_a_patch_without_python_modules_scores_100_without_running_mutmut(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(mutation, "mutmut", lambda *a: pytest.fail("mutmut must not run"))
    assert mutation.main(["--modules"]) == 0
    assert "mutation score 100.0%" in capsys.readouterr().out


def test_a_mutmut_failure_is_an_error_not_a_pass(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    failed = subprocess.CompletedProcess([], 1, "", "boom")
    monkeypatch.setattr(mutation, "mutmut", lambda *a: failed)
    assert mutation.main(["--modules", "pkg.mod*"]) == 2
    assert "mutmut failed" in capsys.readouterr().err


def test_the_score_line_comes_from_the_stats_file(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "mutants").mkdir()
    (tmp_path / "mutants" / "mutmut-cicd-stats.json").write_text(
        json.dumps({"killed": 8, "survived": 2, "total": 500})
    )
    ok = subprocess.CompletedProcess([], 0, "", "")
    monkeypatch.setattr(mutation, "mutmut", lambda *a: ok)
    assert mutation.main(["--modules", "pkg.mod*"]) == 0
    assert "mutation score 80.0%" in capsys.readouterr().out
