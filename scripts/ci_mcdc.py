"""MC/DC over a patch's Python files, measured with pymcdc and the repo's unittest tests.

Run by the `mcdc` check in policy/ci.yaml inside the CI sandbox. pymcdc instruments a source
file, runs `unittest.TestCase` tests against it and reports how many of the condition
combinations MC/DC requires were exercised. It cannot run pytest-style tests, so only test
files that define `unittest.TestCase` classes count; the build agent writes protected tests
that way. Single-condition decisions need no combinations and are not counted.

Prints one line per file and a final `MC/DC <percent>%` line, which the policy compares with
its minimum. A file with no compound decision counts as fully covered. If pymcdc fails or
prints no summary the script exits 2: a broken tool is never a pass.
"""

from __future__ import annotations

import argparse
import ast
import re
import subprocess
import sys
import tempfile
from pathlib import Path

SUMMARY = re.compile(r"Covered (\d+) out of (\d+) requirements")
EMPTY_TESTS = "import unittest\n\n\nclass Empty(unittest.TestCase):\n    pass\n"
TIMEOUT_SECONDS = 600


def defines_test_case(path: Path) -> bool:
    """True if the file has a top-level class derived from `unittest.TestCase`/`TestCase`."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
    except SyntaxError:
        return False
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            for base in node.bases:
                name = base.attr if isinstance(base, ast.Attribute) else getattr(base, "id", "")
                if name == "TestCase":
                    return True
    return False


def unittest_files(tests: Path) -> list[Path]:
    """Test files that define unittest.TestCase classes (the only ones pymcdc can run)."""
    return sorted(p.resolve() for p in tests.rglob("*.py") if defines_test_case(p))


def is_implementation(name: str, tests: Path) -> bool:
    path = Path(name)
    return path.suffix == ".py" and path.parts[0] != tests.name


def measure(name: str, tests: list[Path], scratch: Path) -> tuple[int, int]:
    """(covered, required) combinations for one source file."""
    path = Path(name)
    cwd = Path("src") if path.parts[0] == "src" else Path(".")
    relative = path.relative_to(cwd)
    suite = tests
    if not suite:
        empty = scratch / "test_empty.py"
        empty.write_text(EMPTY_TESTS, encoding="utf-8")
        suite = [empty]
    argv = [sys.executable, "-m", "pymcdc"]
    for test in suite:
        argv += ["--unittest", str(test)]
    done = subprocess.run(  # noqa: S603 - fixed argv; runs inside the CI sandbox
        [*argv, str(relative)],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=TIMEOUT_SECONDS,
        check=False,
    )
    for log in (cwd / relative).parent.glob(f"{relative.name}.mdc"):
        log.unlink()  # pymcdc leaves its execution log next to the source
    found = SUMMARY.search(done.stdout)
    if done.returncode != 0 or found is None:
        sys.stderr.write(f"pymcdc failed on {name}:\n{done.stdout[-1500:]}{done.stderr[-1500:]}\n")
        raise SystemExit(2)
    return int(found.group(1)), int(found.group(2))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--tests", type=Path, required=True)
    parser.add_argument("--files", nargs="*", default=[])
    args = parser.parse_args(argv)
    targets = [f for f in args.files if is_implementation(f, args.tests)]
    tests = unittest_files(args.tests) if args.tests.is_dir() else []
    covered = required = 0
    with tempfile.TemporaryDirectory(prefix="mcdc-") as scratch:
        for name in targets:
            got, need = measure(name, tests, Path(scratch))
            print(f"{name}: {got} of {need} requirements")
            covered, required = covered + got, required + need
    percent = 100.0 if required == 0 else 100.0 * covered / required
    print(f"MC/DC {percent:.1f}% ({covered} of {required} requirements, {len(targets)} files)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
