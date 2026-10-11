"""bwrap sandbox runner for model workers.

The sandbox is default-deny: it sees /usr (plus merged-usr symlinks), the
dynamic-linker cache, private /proc, /dev, /tmp and $HOME, its declared inputs
read-only under /inputs/<artifact_id>, any extra read-only binds, and exactly
one writable root at /work. Every namespace is unshared, so the only network is
a private loopback; the only route to a model is the inference broker over two
inherited pipes, served by the in-sandbox shim (sandbox_entry.py). The
environment is cleared and rebuilt from an allowlist.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import threading
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from agentic_pipeline import sandbox_entry
from agentic_pipeline.broker import BrokerStats, InferenceBroker

WORK = "/work"
INPUTS = "/inputs"
HOME = "/home/worker"
ENTRY = "/opt/agentic/sandbox_entry.py"
ENTRY_HOST = Path(sandbox_entry.__file__).resolve()
BROKER_JOIN_SECONDS = 10.0
USR_LINKS = ("bin", "sbin", "lib", "lib32", "lib64", "libx32")


class SandboxUnavailable(Exception):
    pass


@dataclass(frozen=True, slots=True)
class SandboxLimits:
    """Resource caps. tmpfs sizes are enforced by bwrap; the rest by prlimit (per process)."""

    tmpfs_bytes: int = 256 * 1024 * 1024  # each of /tmp and $HOME (RAM-backed)
    file_bytes: int = 64 * 1024 * 1024  # largest single file a process may write
    cpu_seconds: int = 900
    open_files: int = 1024

    def prlimit_args(self) -> list[str]:
        return [
            f"--fsize={self.file_bytes}",
            f"--cpu={self.cpu_seconds}",
            f"--nofile={self.open_files}",
        ]


@dataclass(frozen=True, slots=True)
class SandboxSpec:
    argv: list[str]
    write_path: Path | None = None  # bound read-write at /work; private tmpfs if None
    inputs: Mapping[str, Path] = field(default_factory=dict)
    ro_binds: Mapping[str, Path] = field(default_factory=dict)  # sandbox path -> host path
    env: Mapping[str, str] = field(default_factory=dict)
    timeout: float = 600.0
    limits: SandboxLimits = field(default_factory=SandboxLimits)


@dataclass(frozen=True, slots=True)
class SandboxResult:
    returncode: int
    stdout: str
    stderr: str
    timed_out: bool
    broker: BrokerStats


def _usr_links() -> list[str]:
    """Recreate the host's top-level /bin, /lib... links so the dynamic loader resolves.

    Merged-usr layouts differ (Arch: /lib64 -> usr/lib; Debian: /lib64 -> usr/lib64),
    so mirror the host rather than hard-coding one. Real directories are bound read-only.
    """
    argv: list[str] = []
    for name in USR_LINKS:
        path = Path("/") / name
        if path.is_symlink():
            argv += ["--symlink", os.readlink(path), str(path)]
        elif path.is_dir():
            argv += ["--ro-bind", str(path), str(path)]
    return argv


def bwrap_argv(bwrap: str, spec: SandboxSpec, req_fd: int, resp_fd: int) -> list[str]:
    argv = [
        bwrap,
        "--die-with-parent",
        "--unshare-all",
        "--new-session",
        "--ro-bind", "/usr", "/usr",
        *_usr_links(),
        "--ro-bind-try", "/etc/ld.so.cache", "/etc/ld.so.cache",
        "--proc", "/proc",
        "--dev", "/dev",
        "--size", str(spec.limits.tmpfs_bytes), "--tmpfs", "/tmp",  # noqa: S108 - private
        "--size", str(spec.limits.tmpfs_bytes), "--tmpfs", HOME,
    ]  # fmt: skip
    if spec.write_path is None:
        argv += ["--size", str(spec.limits.tmpfs_bytes), "--tmpfs", WORK]
    else:
        argv += ["--bind", str(spec.write_path), WORK]
    for artifact_id, path in sorted(spec.inputs.items()):
        argv += ["--ro-bind", str(path), f"{INPUTS}/{artifact_id}"]
    for target, source in sorted(spec.ro_binds.items()):
        argv += ["--ro-bind", str(source), target]
    env = {
        "PATH": "/usr/bin",
        "HOME": HOME,
        "LANG": "C.UTF-8",
        **spec.env,
        sandbox_entry.REQ_FD_ENV: str(req_fd),
        sandbox_entry.RESP_FD_ENV: str(resp_fd),
    }
    argv += ["--ro-bind", str(ENTRY_HOST), ENTRY, "--chdir", WORK, "--clearenv"]
    for key, value in env.items():
        argv += ["--setenv", key, value]
    return [*argv, "python3", "-I", ENTRY, "--", *spec.argv]


def sandbox_command(
    bwrap: str, prlimit: str, spec: SandboxSpec, req_fd: int, resp_fd: int
) -> list[str]:
    return [prlimit, *spec.limits.prlimit_args(), "--", *bwrap_argv(bwrap, spec, req_fd, resp_fd)]


def run_sandboxed(
    spec: SandboxSpec, broker: InferenceBroker, bwrap: str | None = None
) -> SandboxResult:
    bwrap = bwrap or shutil.which("bwrap")
    prlimit = shutil.which("prlimit")
    if bwrap is None or prlimit is None:
        missing = [name for name, path in (("bwrap", bwrap), ("prlimit", prlimit)) if not path]
        raise SandboxUnavailable(f"not found on PATH: {missing}")
    req_r, req_w = os.pipe()
    resp_r, resp_w = os.pipe()
    try:
        proc = subprocess.Popen(  # noqa: S603 - fixed argv built above
            sandbox_command(bwrap, prlimit, spec, req_w, resp_r),
            pass_fds=(req_w, resp_r),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env={"PATH": os.environ.get("PATH", "/usr/bin")},
            start_new_session=True,
        )
    except OSError as exc:
        os.close(req_r)
        os.close(resp_w)
        raise SandboxUnavailable(str(exc)) from exc
    finally:
        # Close the child's ends here so the broker sees EOF when the sandbox exits.
        os.close(req_w)
        os.close(resp_r)

    serving = threading.Thread(target=broker.serve, args=(req_r, resp_w), daemon=True)
    serving.start()
    timed_out = False
    try:
        stdout, stderr = proc.communicate(timeout=spec.timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        proc.kill()
        stdout, stderr = proc.communicate()
    serving.join(BROKER_JOIN_SECONDS)
    os.close(req_r)
    os.close(resp_w)
    return SandboxResult(proc.returncode, stdout, stderr, timed_out, broker.stats)
