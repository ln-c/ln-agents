"""Integration tests: a fake worker inside the real bwrap sandbox (skipped without bwrap)."""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from agentic_pipeline.broker import BrokerPolicy, InferenceBroker
from agentic_pipeline.sandbox import (
    ENTRY,
    HOME,
    INPUTS,
    WORK,
    SandboxLimits,
    SandboxSpec,
    bwrap_argv,
    run_sandboxed,
    sandbox_command,
)

MODEL = "qwen3:8b"


def _bwrap_usable() -> bool:
    bwrap = shutil.which("bwrap")
    if bwrap is None:
        return False
    argv = bwrap_argv(bwrap, SandboxSpec(argv=["true"]), 0, 1)
    probe = argv[: argv.index("python3")] + ["/usr/bin/true"]
    return subprocess.run(probe, capture_output=True, check=False).returncode == 0  # noqa: S603


pytestmark = pytest.mark.skipif(not _bwrap_usable(), reason="bwrap unavailable or unusable")

WORKER = r"""
import json, os, socket, sys, urllib.error, urllib.request
out = {}
def attempt(name, fn):
    try:
        out[name] = fn()
    except Exception as exc:
        out[name] = f"denied: {type(exc).__name__}"
def post(model, path="/chat/completions"):
    req = urllib.request.Request(os.environ["AP_INFERENCE_URL"] + path,
        data=json.dumps({"model": model}).encode(), method="POST",
        headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return [r.status, r.read().decode()]
    except urllib.error.HTTPError as e:
        return [e.code, ""]
def write(path):
    with open(path, "w") as fh:
        fh.write("x")
    return "written"
attempt("brokered", lambda: post(MODEL))
attempt("foreign_model", lambda: post("llama3"))
attempt("other_path", lambda: post(MODEL, "/../api/pull"))
attempt("work_write", lambda: write("/work/out.txt"))
attempt("input_write", lambda: write("/inputs/spec/new.txt"))
attempt("input_read", lambda: open("/inputs/spec/spec.md").read())
attempt("host_home", lambda: os.listdir(HOST_HOME))
attempt("host_ollama", lambda: socket.create_connection(("127.0.0.1", 11434), 2) and "open")
attempt("dns", lambda: socket.getaddrinfo("example.com", 443) and "resolved")
attempt("broker_fds", lambda: sorted(k for k in os.environ if k.startswith("AP_BROKER")))
attempt("env_keys", lambda: sorted(os.environ))
print(json.dumps(out))
"""


def _run(tmp_path: Path, worker: str = WORKER, **policy: object) -> tuple[dict, object]:
    work, spec_dir = tmp_path / "work", tmp_path / "spec"
    work.mkdir()
    spec_dir.mkdir()
    (spec_dir / "spec.md").write_text("contract")
    script = worker.replace("MODEL", repr(MODEL)).replace("HOST_HOME", repr(str(Path.home())))
    broker = InferenceBroker(
        BrokerPolicy(MODEL, **policy),  # type: ignore[arg-type]
        lambda path, body: (200, {"Content-Type": "text/plain"}, iter([b"hello ", b"worker"])),
    )
    result = run_sandboxed(
        SandboxSpec(
            argv=["python3", "-I", "-c", script],
            write_path=work,
            inputs={"spec": spec_dir},
            timeout=60,
        ),
        broker,
    )
    assert result.returncode == 0, result.stderr
    return json.loads(result.stdout.strip().splitlines()[-1]), result


def test_worker_reaches_model_only_through_broker(tmp_path: Path) -> None:
    out, result = _run(tmp_path)
    assert out["brokered"] == [200, "hello worker"]
    assert out["foreign_model"][0] == 403
    assert out["other_path"][0] == 403
    assert out["host_ollama"].startswith("denied")
    assert out["dns"].startswith("denied")
    assert result.broker.rounds == 1 and len(result.broker.rejected) == 2


def test_filesystem_is_default_deny(tmp_path: Path) -> None:
    out, _ = _run(tmp_path)
    assert out["work_write"] == "written" and (tmp_path / "work" / "out.txt").exists()
    assert out["input_read"] == "contract"
    assert out["input_write"].startswith("denied")
    assert not (tmp_path / "spec" / "new.txt").exists()
    assert out["host_home"].startswith("denied")


def test_worker_never_sees_broker_fds_or_host_env(tmp_path: Path) -> None:
    out, _ = _run(tmp_path)
    assert out["broker_fds"] == []
    assert set(out["env_keys"]) <= {"AP_INFERENCE_URL", "HOME", "LANG", "PATH", "PWD"}


def test_round_limit_applies_inside_sandbox(tmp_path: Path) -> None:
    out, result = _run(tmp_path, max_rounds=0)
    assert out["brokered"][0] == 429 and result.broker.rounds == 0


def test_timeout_kills_worker(tmp_path: Path) -> None:
    broker = InferenceBroker(BrokerPolicy(MODEL), lambda p, b: (200, {}, iter([])))
    result = run_sandboxed(
        SandboxSpec(argv=["python3", "-c", "import time; time.sleep(60)"], timeout=2), broker
    )
    assert result.timed_out


def test_argv_layout() -> None:
    argv = bwrap_argv(
        "/usr/bin/bwrap",
        SandboxSpec(argv=["pi"], write_path=Path("/run/w"), inputs={"spec": Path("/run/s")}),
        5,
        6,
    )
    assert argv[argv.index("--bind") + 1 : argv.index("--bind") + 3] == ["/run/w", WORK]
    assert f"{INPUTS}/spec" in argv and "--clearenv" in argv and "--unshare-all" in argv
    assert argv[-5:] == ["python3", "-I", ENTRY, "--", "pi"]
    assert ["--setenv", "HOME", HOME] == argv[argv.index("HOME") - 1 : argv.index("HOME") + 2]
    assert "/" not in [argv[i + 1] for i, a in enumerate(argv) if a in ("--bind", "--ro-bind")]


def _limited(tmp_path: Path, script: str, **limits: int) -> object:
    broker = InferenceBroker(BrokerPolicy(MODEL), lambda p, b: (200, {}, iter([])))
    spec = SandboxSpec(
        argv=["python3", "-I", "-c", script],
        write_path=tmp_path,
        limits=SandboxLimits(**limits),
        timeout=60,
    )
    return run_sandboxed(spec, broker)


def test_file_size_limit_enforced(tmp_path: Path) -> None:
    script = (
        "import sys\n"
        "try:\n open('/work/big.bin','wb').write(b'x' * 2_000_000); print('wrote')\n"
        "except OSError as e: print('blocked', e.errno)"
    )
    result = _limited(tmp_path, script, file_bytes=1_000_000)
    assert result.stdout.strip().startswith("blocked"), result.stdout
    assert (tmp_path / "big.bin").stat().st_size <= 1_000_000


def test_tmpfs_size_limit_enforced(tmp_path: Path) -> None:
    script = (
        "try:\n"
        " f = open('/tmp/fill.bin','wb')\n"
        " [f.write(b'x' * 1_000_000) for _ in range(8)]; f.flush(); print('filled')\n"
        "except OSError as e: print('blocked', e.errno)"
    )
    result = _limited(tmp_path, script, tmpfs_bytes=4_000_000)
    assert result.stdout.strip().startswith("blocked"), result.stdout


def test_command_wraps_bwrap_in_prlimit() -> None:
    spec = SandboxSpec(argv=["true"], limits=SandboxLimits(file_bytes=5, cpu_seconds=6))
    cmd = sandbox_command("/usr/bin/bwrap", "/usr/bin/prlimit", spec, 3, 4)
    assert cmd[:5] == ["/usr/bin/prlimit", "--fsize=5", "--cpu=6", "--nofile=1024", "--"]
    assert cmd[5] == "/usr/bin/bwrap"
    assert cmd.count("--size") == 3  # /tmp, $HOME, and the tmpfs /work
