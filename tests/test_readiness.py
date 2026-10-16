"""Tests for the read-only readiness preflight."""

from __future__ import annotations

import copy
import dataclasses
import json
import shutil
import socket
import threading
from collections.abc import Iterator, Mapping
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import pytest

from agentic_pipeline.broker import BrokerStats
from agentic_pipeline.cli import main
from agentic_pipeline.graph import load_yaml
from agentic_pipeline.readiness import (
    SANDBOX_CHECKS,
    Check,
    Host,
    Report,
    Status,
    evaluate_stage,
    parse_sandbox_probe,
    probe_credential_env,
    probe_executable,
    probe_listeners,
    probe_ollama,
    probe_sandbox,
    run_preflight,
)
from agentic_pipeline.sandbox import SandboxResult, SandboxUnavailable

ROOT = Path(__file__).resolve().parents[1]
REGISTRY = load_yaml(ROOT / "policy" / "model_registry.yaml")
CLAUDE = REGISTRY["providers"]["claude_pro"]["readiness"]
GOOGLE = REGISTRY["providers"]["google_ai_pro"]["readiness"]
QWEN = REGISTRY["providers"]["qwen_local"]["readiness"]


def fake_host(tmp_path: Path, output: str = "", code: int = 0, found: bool = True) -> Host:
    return Host(
        environ={"PATH": "/usr/bin"},
        home=tmp_path,
        proc_root=tmp_path / "proc",
        which=lambda name: f"/fake/{name}" if found else None,
        run=lambda argv, env: (code, output),
    )


# ── Credential environment ───────────────────────────────────────────────────


def test_credential_env_absent() -> None:
    check = probe_credential_env(["ANTHROPIC_API_KEY"], {"PATH": "/usr/bin"})
    assert check.status is Status.VERIFIED


def test_credential_env_present_even_if_empty() -> None:
    check = probe_credential_env(["ANTHROPIC_API_KEY"], {"ANTHROPIC_API_KEY": ""})
    assert check.status is Status.FAILED and "ANTHROPIC_API_KEY" in check.detail


# ── Executables ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("spec", "output", "code", "found", "expected"),
    [
        (CLAUDE, "2.1.296 (Claude Code)\n", 0, True, Status.VERIFIED),
        (CLAUDE, "2.0.9 (Claude Code)\n", 0, True, Status.FAILED),
        (CLAUDE, "2.1.296\n", 0, True, Status.FAILED),
        (CLAUDE, "2.1.296 (Claude Code)\n", 1, True, Status.FAILED),
        (CLAUDE, "", 0, False, Status.FAILED),
        (GOOGLE, "1.3.3\n", 0, True, Status.VERIFIED),
        (GOOGLE, "1.3.0\n", 0, True, Status.FAILED),
    ],
    ids=["claude-ok", "claude-old", "claude-bad-format", "claude-nonzero", "claude-missing",
         "agy-ok", "agy-old"],
)  # fmt: skip
def test_probe_executable(
    tmp_path: Path, spec: Mapping[str, Any], output: str, code: int, found: bool, expected: Status
) -> None:
    check = probe_executable(spec, fake_host(tmp_path, output, code, found))
    assert check.status is expected, check


def test_probe_executable_passes_allowlisted_env(tmp_path: Path) -> None:
    seen: dict[str, str] = {}

    def run(argv: list[str], env: Mapping[str, str]) -> tuple[int, str]:
        seen.update(env)
        return 0, "1.3.3\n"

    host = dataclasses.replace(
        fake_host(tmp_path), environ={"PATH": "/usr/bin", "GEMINI_API_KEY": "x"}, run=run
    )
    probe_executable(GOOGLE, host)
    assert set(seen) == {"HOME", "LANG", "PATH"}


# ── Ollama endpoint ──────────────────────────────────────────────────────────


@pytest.fixture
def ollama() -> Iterator[dict[str, Any]]:
    state: dict[str, Any] = {"status": 200, "body": b""}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - http.server API
            self.send_response(state["status"])
            self.end_headers()
            self.wfile.write(state["body"])

        def log_message(self, *args: object) -> None:
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    state["spec"] = {**QWEN, "port": server.server_address[1]}
    yield state
    server.shutdown()
    server.server_close()


def test_probe_ollama_reports_each_model_separately(ollama: dict[str, Any]) -> None:
    ollama.update(status=200, body=_tags("qwen3:8b"))
    _, models = probe_ollama(ollama["spec"], {"qwen3_8b": "qwen3:8b", "other": "llama3"})
    assert {m: c.status for m, c in models.items()} == {
        "qwen3_8b": Status.VERIFIED,
        "other": Status.FAILED,
    }


def _tags(*names: str) -> bytes:
    return json.dumps({"models": [{"name": n} for n in names]}).encode()


@pytest.mark.parametrize(
    ("status", "body", "daemon", "inventory"),
    [
        (200, _tags("qwen3:8b"), Status.VERIFIED, Status.VERIFIED),
        (200, _tags("qwen3:8b-instruct", "llama3"), Status.VERIFIED, Status.FAILED),
        (200, b"<html>", Status.VERIFIED, Status.FAILED),
        (500, b"", Status.FAILED, Status.UNVERIFIED),
    ],
    ids=["exact-match", "no-exact-match", "malformed", "http-error"],
)
def test_probe_ollama(
    ollama: dict[str, Any], status: int, body: bytes, daemon: Status, inventory: Status
) -> None:
    ollama.update(status=status, body=body)
    provider, models = probe_ollama(ollama["spec"], {"qwen3_8b": "qwen3:8b"})
    assert [c.status for c in provider] == [daemon]
    assert [c.status for c in models.values()] == [inventory], models


def test_probe_ollama_stopped() -> None:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    provider, models = probe_ollama({**QWEN, "port": port}, {"qwen3_8b": "qwen3:8b"})
    assert [c.status for c in provider] == [Status.FAILED]
    assert [c.status for c in models.values()] == [Status.UNVERIFIED]


# ── Listener binding (/proc/net/tcp, /proc/net/tcp6) ─────────────────────────

V4_LOOPBACK = "0100007F"
V4_ANY = "00000000"
V6_ANY = "0" * 32
V6_LOOPBACK = "00000000000000000000000001000000"
V6_MAPPED_LOOPBACK = "0000000000000000FFFF00000100007F"
HEADER = "  sl  local_address rem_address   st tx_queue rx_queue\n"


def _proc(tmp_path: Path, tcp: list[str], tcp6: list[str]) -> Path:
    net = tmp_path / "proc" / "net"
    net.mkdir(parents=True)
    for name, rows in (("tcp", tcp), ("tcp6", tcp6)):
        lines = [f"   {i}: {row} 00000000:0000 0A 00000000:00000000" for i, row in enumerate(rows)]
        (net / name).write_text(HEADER + "\n".join(lines) + "\n", encoding="ascii")
    return tmp_path / "proc"


@pytest.mark.parametrize(
    ("tcp", "tcp6", "expected"),
    [
        ([f"{V4_LOOPBACK}:2CAA"], [], Status.VERIFIED),
        ([], [f"{V6_LOOPBACK}:2CAA"], Status.VERIFIED),
        ([], [f"{V6_MAPPED_LOOPBACK}:2CAA"], Status.VERIFIED),
        ([f"{V4_ANY}:2CAA"], [], Status.FAILED),
        ([f"{V4_LOOPBACK}:2CAA"], [f"{V6_ANY}:2CAA"], Status.FAILED),
        ([f"{V4_ANY}:0050"], [], Status.UNVERIFIED),
    ],
    ids=["v4-loopback", "v6-loopback", "v4-mapped-loopback", "v4-wildcard",
         "v6-wildcard-alongside-loopback", "other-port-only"],
)  # fmt: skip
def test_probe_listeners(tmp_path: Path, tcp: list[str], tcp6: list[str], expected: Status) -> None:
    assert probe_listeners(11434, _proc(tmp_path, tcp, tcp6)).status is expected


def test_probe_listeners_ignores_non_listening_sockets(tmp_path: Path) -> None:
    proc = _proc(tmp_path, [f"{V4_LOOPBACK}:2CAA"], [])
    tcp = proc / "net" / "tcp"
    tcp.write_text(
        tcp.read_text() + f"   9: {V4_ANY}:2CAA 0100007F:DC62 01 00000000:00000000\n",
        encoding="ascii",
    )
    assert probe_listeners(11434, proc).status is Status.VERIFIED


# ── Sandbox self-test ────────────────────────────────────────────────────────

GOOD_PROBE = {
    "write_root_writable": True,
    "input_writable": False,
    "host_home_visible": False,
    "interfaces": ["lo"],
    "brokered": [200, "probe-ok"],
    "foreign_model": [403, ""],
}


def _probe_result(
    changes: Mapping[str, Any] | None = None, code: int = 0, rounds: int = 1, stdout: str = ""
) -> SandboxResult:
    out = stdout or json.dumps({**GOOD_PROBE, **(changes or {})})
    return SandboxResult(code, out, "", False, BrokerStats(rounds=rounds))


def _verified(checks: Mapping[str, Check]) -> dict[str, bool]:
    return {name: c.status is Status.VERIFIED for name, c in checks.items()}


ALL_OK = dict.fromkeys(SANDBOX_CHECKS, True)


@pytest.mark.parametrize(
    ("changes", "rounds", "failing"),
    [
        ({}, 1, set()),
        ({"write_root_writable": False}, 1, {"sandbox_enforcement"}),
        ({"host_home_visible": True}, 1, {"sandbox_enforcement"}),
        ({"input_writable": True}, 1, {"protected_paths_read_only"}),
        ({"interfaces": ["eth0", "lo"]}, 1, {"network_disabled"}),
        ({"brokered": [502, ""]}, 1, {"broker_mediated_inference"}),
        ({"foreign_model": [200, "leak"]}, 1, {"broker_mediated_inference"}),
        ({}, 2, {"broker_mediated_inference"}),
    ],
    ids=["enforced", "no-write-root", "home-visible", "input-writable", "network-visible",
         "broker-failed", "foreign-model-allowed", "extra-rounds"],
)  # fmt: skip
def test_parse_sandbox_probe(changes: dict, rounds: int, failing: set[str]) -> None:
    checks = parse_sandbox_probe(_probe_result(changes, rounds=rounds))
    assert _verified(checks) == {n: n not in failing for n in SANDBOX_CHECKS}


@pytest.mark.parametrize(
    "result",
    [
        SandboxResult(1, "", "bwrap: setting up uid map: Permission denied", False, BrokerStats()),
        SandboxResult(-9, "", "", True, BrokerStats()),
        _probe_result(stdout="not json"),
    ],
    ids=["nonzero-exit", "timed-out", "garbage-output"],
)
def test_incomplete_self_test_is_never_verified(result: SandboxResult) -> None:
    checks = parse_sandbox_probe(result)
    assert checks["sandbox_enforcement"].status is Status.FAILED
    assert not any(_verified(checks).values())


def test_sandbox_missing_bwrap(tmp_path: Path) -> None:
    checks = probe_sandbox(fake_host(tmp_path, found=False))
    assert all(c.status is Status.UNVERIFIED for c in checks.values())


def test_sandbox_start_failure(tmp_path: Path) -> None:
    def refuse(*args: object) -> SandboxResult:
        raise SandboxUnavailable("no user namespaces")

    checks = probe_sandbox(dataclasses.replace(fake_host(tmp_path), run_sandbox=refuse))
    assert checks["sandbox_enforcement"].status is Status.FAILED


@pytest.mark.skipif(shutil.which("bwrap") is None, reason="bwrap not installed")
def test_sandbox_real_bwrap_smoke() -> None:
    checks = probe_sandbox(Host.real())
    assert set(checks) == set(SANDBOX_CHECKS)


# ── Stage eligibility ────────────────────────────────────────────────────────

ISOLATION = (
    "sandbox_enforcement",
    "protected_paths_read_only",
    "network_disabled",
    "proposal_path_layout",
    "session_separation",
    "sealed_input_verification",
    "broker_mediated_inference",
)


def ready_report() -> Report:
    """A report where every check is VERIFIED; tests downgrade individual checks."""
    ok = Status.VERIFIED
    return Report(
        static_errors=[],
        global_checks=[Check("credential_env_absent", ok, "")],
        providers={
            "claude_pro": [Check("executable", ok, "")],
            "google_ai_pro": [Check("executable", ok, "")],
            "qwen_local": [Check("daemon", ok, ""), Check("listener_binding", ok, "")],
        },
        models={"qwen3_8b": [Check("model_inventory", ok, "")]},
        isolation={name: Check(name, ok, "") for name in ISOLATION},
        registry=REGISTRY,
    )


def _downgrade(checks: list[Check], name: str, status: Status = Status.UNVERIFIED) -> None:
    i = next(i for i, c in enumerate(checks) if c.name == name)
    checks[i] = Check(name, status, "")


def test_all_verified_a1_eligible_with_role_backends() -> None:
    verdict = evaluate_stage("A1", ready_report())
    assert verdict.eligible, verdict.reasons
    assert verdict.backends == {
        "proposal_a": "claude_cli_default",
        "proposal_b": "gemini_3_8_flash_high",
    }


def test_a1_blocked_when_one_cloud_agent_unavailable() -> None:
    report = ready_report()
    _downgrade(report.providers["google_ai_pro"], "executable")
    verdict = evaluate_stage("A1", report)
    assert not verdict.eligible
    assert "proposal_b: gemini_3_8_flash_high.executable UNVERIFIED" in verdict.reasons


def test_a3_falls_back_in_declared_order() -> None:
    report = ready_report()
    _downgrade(report.providers["claude_pro"], "executable")
    verdict = evaluate_stage("A3", report)
    assert verdict.eligible and verdict.backends == {"synthesis": "gemini_3_8_flash_high"}


def test_a_model_missing_from_the_local_inventory_blocks_build() -> None:
    report = ready_report()
    _downgrade(report.models["qwen3_8b"], "model_inventory", Status.FAILED)
    verdict = evaluate_stage("E1", report)
    assert not verdict.eligible
    assert "implementation_patch: qwen3_8b.model_inventory FAILED" in verdict.reasons


def test_isolation_check_blocks_stage() -> None:
    report = ready_report()
    report.isolation["broker_mediated_inference"] = Check(
        "broker_mediated_inference", Status.UNVERIFIED, ""
    )
    verdict = evaluate_stage("E1", report)
    assert not verdict.eligible
    assert "implementation_patch: isolation broker_mediated_inference UNVERIFIED" in (
        verdict.reasons
    )
    assert evaluate_stage("C1", report).eligible  # C1 does not require the broker


def test_credential_env_blocks_every_stage() -> None:
    report = ready_report()
    _downgrade(report.global_checks, "credential_env_absent", Status.FAILED)
    for stage in ("A1", "A2", "A3", "C1", "D1", "E1"):
        assert not evaluate_stage(stage, report).eligible, stage


@pytest.mark.parametrize(
    ("stage", "reason"),
    [("E4", "stage disabled"), ("BENCH", "stage disabled"), ("G1", "unknown or non-model")],
)
def test_stage_not_dispatchable(stage: str, reason: str) -> None:
    verdict = evaluate_stage(stage, ready_report())
    assert not verdict.eligible and verdict.reasons[0].startswith(reason)


def test_static_errors_block_and_skip_probes(tmp_path: Path) -> None:
    for name in ("pipeline", "policy", "schemas", "agents"):
        shutil.copytree(ROOT / name, tmp_path / name)
    (tmp_path / "policy" / "weights.yaml").write_text("version: 1\nweights: {}\n")

    def refuse(*args: object) -> Any:
        raise AssertionError("host probed despite static validation failure")

    host = Host(environ={}, home=tmp_path, which=refuse, run=refuse)
    report = run_preflight(tmp_path, host)
    assert report.static_errors
    assert not evaluate_stage("A1", report).eligible


# ── CLI ──────────────────────────────────────────────────────────────────────


def test_cli_preflight_unknown_stage_exits_1(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["preflight", "--stage", "NOPE"]) == 1
    assert "stage NOPE: BLOCKED" in capsys.readouterr().err


def test_cli_preflight_disabled_stage_exits_1(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["preflight", "--stage", "E4"]) == 1
    assert "stage disabled" in capsys.readouterr().err


def test_preflight_evaluates_the_registry_it_is_given(tmp_path: Path) -> None:
    frozen = copy.deepcopy(REGISTRY)
    frozen["stage_requirements"]["A3"] = {"mode": "disabled", "reason": "frozen for this run"}
    host = Host(environ={}, home=tmp_path, which=lambda name: None)
    report = run_preflight(ROOT, host, registry=frozen)
    assert report.registry is frozen
    verdict = evaluate_stage("A3", report)
    assert not verdict.eligible and "frozen for this run" in verdict.reasons[0]
