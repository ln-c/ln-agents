"""Mutation score over a patch's modules, measured with mutmut.

Run by the `mutation` check in policy/ci.yaml inside the CI sandbox. `--modules` takes the
module globs of the patch's Python files (`pkg.mod*`); mutmut mutates only those and runs the
test suite against each mutant. The score is killed mutants over the mutants that were run.
Prints a final `mutation score <percent>%` line, which the policy compares with its minimum.
A patch with no Python module, or whose modules produce no mutants, scores 100. If mutmut
fails the script exits 2: a broken tool is never a pass.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

STATS = Path("mutants") / "mutmut-cicd-stats.json"
TIMEOUT_SECONDS = 3000


def mutmut(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(  # noqa: S603 - fixed argv; runs inside the CI sandbox
        [sys.executable, "-m", "mutmut", *args],
        capture_output=True,
        text=True,
        timeout=TIMEOUT_SECONDS,
        check=False,
    )


# Every outcome of a mutant that was actually run. `total` also counts the mutants of modules
# that were not selected, so it is not the denominator.
RUN_OUTCOMES = ("killed", "survived", "timeout", "suspicious", "no_tests", "segfault")


def score(stats: dict[str, int]) -> float:
    run = sum(stats.get(k, 0) for k in RUN_OUTCOMES)
    return 100.0 if run == 0 else 100.0 * stats["killed"] / run


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--modules", nargs="*", default=[])
    args = parser.parse_args(argv)
    if not args.modules:
        print("mutation score 100.0% (no Python modules in the patch)")
        return 0
    ran = mutmut("run", *args.modules)
    exported = mutmut("export-cicd-stats") if ran.returncode == 0 else ran
    if exported.returncode != 0 or not STATS.is_file():
        tail = (ran.stdout + ran.stderr + exported.stdout + exported.stderr)[-2000:]
        sys.stderr.write(f"mutmut failed:\n{tail}\n")
        return 2
    stats = json.loads(STATS.read_text(encoding="utf-8"))
    run = sum(stats.get(k, 0) for k in RUN_OUTCOMES)
    print(f"mutation {stats['killed']} killed of {run} run ({stats['survived']} survived)")
    print(f"mutation score {score(stats):.1f}% ({len(args.modules)} module patterns)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
