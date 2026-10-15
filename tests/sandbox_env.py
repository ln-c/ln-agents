"""Shared test helpers: is bwrap usable here, and a host-process stand-in for it."""

from __future__ import annotations

import shutil
import subprocess
from typing import Any

from agentic_pipeline.broker import BrokerStats
from agentic_pipeline.sandbox import SandboxResult, SandboxSpec, bwrap_argv


def bwrap_usable() -> bool:
    bwrap = shutil.which("bwrap")
    if bwrap is None:
        return False
    argv = bwrap_argv(bwrap, SandboxSpec(argv=["true"]), 0, 1)
    probe = argv[: argv.index("python3")] + ["/usr/bin/true"]
    return subprocess.run(probe, capture_output=True, check=False).returncode == 0  # noqa: S603


BWRAP_USABLE = bwrap_usable()


def run_on_host(spec: SandboxSpec, broker: Any = None) -> SandboxResult:
    """Stand-in for `run_sandboxed` for gate-logic tests: runs the argv in the tree."""
    try:
        done = subprocess.run(  # noqa: S603 - test commands only
            spec.argv,
            cwd=spec.write_path,
            capture_output=True,
            text=True,
            timeout=spec.timeout,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return SandboxResult(-9, "", "", True, BrokerStats())
    return SandboxResult(done.returncode, done.stdout, done.stderr, False, BrokerStats())
