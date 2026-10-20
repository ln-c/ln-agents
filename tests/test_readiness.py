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

from agentic_pipeline.briefs import BEGIN, END
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
    llamacpp_model_version,
    ovms_model_version,
    parse_sandbox_probe,
    probe_credential_env,
    probe_executable,
    probe_listeners,
    probe_llamacpp,
    probe_ovms,
    probe_sandbox,
    run_preflight,
    run_preflight_tolerating_gaps,
)
from agentic_pipeline.sandbox import SandboxResult, SandboxUnavailable

ROOT = Path(__file__).resolve().parents[1]
REGISTRY = load_yaml(ROOT / "policy" / "model_registry.yaml")
CLAUDE = REGISTRY["providers"]["claude_pro"]["readiness"]
GOOGLE = REGISTRY["providers"]["google_ai_pro"]["readiness"]
LLAMACPP = REGISTRY["providers"]["llamacpp_local"]["readiness"]


def fake_host(tmp_path: Path, output: str = "", code: int = 0, found: bool = True) -> Host:
    return Host(
        environ={"PATH": "/usr/bin"},
        home=tmp_path,
        proc_root=tmp_path / "proc",
        platform="linux",  # the same probes on every OS the suite runs on
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


# ── llama.cpp server endpoint ────────────────────────────────────────────────

SERVED = "qwen3.5-9b"


@pytest.fixture
def llamacpp() -> Iterator[dict[str, Any]]:
    state: dict[str, Any] = {
        "health": 200,
        "models": (200, b'{"data": []}'),
        "props": (200, b"{}"),
    }

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - http.server API
            if self.path == "/health":
                self.send_response(state["health"])
                self.end_headers()
            elif self.path in ("/v1/models", "/props"):
                code, body = state["models" if self.path == "/v1/models" else "props"]
                self.send_response(code)
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_response(404)
                self.end_headers()

        def log_message(self, *args: object) -> None:
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    state["spec"] = {**LLAMACPP, "port": server.server_address[1]}
    yield state
    server.shutdown()
    server.server_close()


def _models(*names: str) -> tuple[int, bytes]:
    return 200, json.dumps({"object": "list", "data": [{"id": n} for n in names]}).encode()


def test_probe_llamacpp_reports_each_model_separately(llamacpp: dict[str, Any]) -> None:
    llamacpp["models"] = _models(SERVED)
    _, models = probe_llamacpp(llamacpp["spec"], {"qwen3_5_9b": SERVED, "other": "llama3"})
    assert {m: c.status for m, c in models.items()} == {
        "qwen3_5_9b": Status.VERIFIED,
        "other": Status.FAILED,
    }


@pytest.mark.parametrize(
    ("health", "models", "daemon", "inventory"),
    [
        (200, _models(SERVED), Status.VERIFIED, Status.VERIFIED),
        (200, _models("qwen3.5-9b-instruct", "llama3"), Status.VERIFIED, Status.FAILED),
        (200, (200, b"<html>"), Status.VERIFIED, Status.FAILED),
        (200, (500, b""), Status.VERIFIED, Status.UNVERIFIED),
        (503, _models(SERVED), Status.FAILED, Status.UNVERIFIED),
        (500, _models(SERVED), Status.FAILED, Status.UNVERIFIED),
    ],
    ids=["exact-match", "no-exact-match", "malformed", "models-error", "loading", "health-error"],
)
def test_probe_llamacpp(
    llamacpp: dict[str, Any],
    health: int,
    models: tuple[int, bytes],
    daemon: Status,
    inventory: Status,
) -> None:
    llamacpp.update(health=health, models=models)
    provider, found = probe_llamacpp(llamacpp["spec"], {"qwen3_5_9b": SERVED})
    assert [c.status for c in provider] == [daemon]
    assert [c.status for c in found.values()] == [inventory], found


def test_probe_llamacpp_says_when_the_model_is_still_loading(llamacpp: dict[str, Any]) -> None:
    llamacpp["health"] = 503
    provider, _ = probe_llamacpp(llamacpp["spec"], {"qwen3_5_9b": SERVED})
    assert "loading" in provider[0].detail


def test_probe_llamacpp_stopped() -> None:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    provider, models = probe_llamacpp({**LLAMACPP, "port": port}, {"qwen3_5_9b": SERVED})
    assert [c.status for c in provider] == [Status.FAILED]
    assert [c.status for c in models.values()] == [Status.UNVERIFIED]


def _props(model_path: Path, build: str = "b11429-d812350493") -> tuple[int, bytes]:
    return 200, json.dumps({"model_path": str(model_path), "build_info": build}).encode()


def test_llamacpp_version_is_the_hash_of_the_model_file_and_the_build(
    llamacpp: dict[str, Any], tmp_path: Path
) -> None:
    import hashlib

    gguf = tmp_path / "model.gguf"
    gguf.write_bytes(b"GGUF weights")
    llamacpp.update(models=_models(SERVED), props=_props(gguf))
    expected = hashlib.sha256(b"GGUF weights").hexdigest()
    assert llamacpp_model_version(llamacpp["spec"], SERVED) == (
        f"{SERVED}@sha256:{expected} (llama.cpp b11429-d812350493)"
    )


def test_llamacpp_version_follows_a_changed_model_file(
    llamacpp: dict[str, Any], tmp_path: Path
) -> None:
    gguf = tmp_path / "model.gguf"
    gguf.write_bytes(b"first")
    llamacpp.update(models=_models(SERVED), props=_props(gguf))
    before = llamacpp_model_version(llamacpp["spec"], SERVED)
    gguf.write_bytes(b"second, and longer")
    assert llamacpp_model_version(llamacpp["spec"], SERVED) != before


@pytest.mark.parametrize(
    ("models", "props", "message"),
    [
        (_models("other"), None, "not served"),
        ((500, b""), None, "returned HTTP 500"),
        ((200, b"<html>"), None, "malformed /v1/models"),
        (None, (500, b""), "GET /props returned HTTP 500"),
        (None, (200, b"[]"), "malformed /props"),
        (None, "unreadable", "cannot read the model file"),
    ],
    ids=[
        "other-model",
        "models-error",
        "models-malformed",
        "props-error",
        "props-malformed",
        "no-file",
    ],
)
def test_llamacpp_version_claims_nothing_it_cannot_show(
    llamacpp: dict[str, Any],
    tmp_path: Path,
    models: tuple[int, bytes] | None,
    props: Any,
    message: str,
) -> None:
    llamacpp["models"] = models or _models(SERVED)
    llamacpp["props"] = (
        _props(tmp_path / "missing.gguf") if props == "unreadable" else props or _props(tmp_path)
    )
    with pytest.raises(LookupError, match=message):
        llamacpp_model_version(llamacpp["spec"], SERVED)


# ── OpenVINO Model Server endpoint ───────────────────────────────────────────

OPENVINO = REGISTRY["providers"]["openvino_local"]["readiness"]
OV_MODEL = "OpenVINO/Qwen3-8B-int4-cw-ov"


@pytest.fixture
def ovms() -> Iterator[dict[str, Any]]:
    state: dict[str, Any] = {"ready": 200, "models": (200, b'{"data": []}')}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - http.server API
            if self.path == "/v2/health/ready":
                self.send_response(state["ready"])
                self.end_headers()
            elif self.path == "/v3/models":
                code, body = state["models"]
                self.send_response(code)
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_response(404)
                self.end_headers()

        def log_message(self, *args: object) -> None:
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    state["spec"] = {**OPENVINO, "port": server.server_address[1]}
    yield state
    server.shutdown()
    server.server_close()


def _served(*names: str) -> tuple[int, bytes]:
    return 200, json.dumps({"object": "list", "data": [{"id": n} for n in names]}).encode()


def test_probe_ovms_reports_each_model_separately(ovms: dict[str, Any]) -> None:
    ovms["models"] = _served(OV_MODEL)
    provider, models = probe_ovms(ovms["spec"], {"a": OV_MODEL, "b": "OpenVINO/other"})
    assert [c.status for c in provider] == [Status.VERIFIED]
    assert {m: c.status for m, c in models.items()} == {"a": Status.VERIFIED, "b": Status.FAILED}


@pytest.mark.parametrize(
    ("ready", "models", "daemon", "inventory"),
    [
        (503, _served(OV_MODEL), Status.FAILED, Status.UNVERIFIED),
        (200, (500, b""), Status.VERIFIED, Status.UNVERIFIED),
        (200, (200, b"<html>"), Status.VERIFIED, Status.FAILED),
        (200, (200, b'{"data": [{"name": "x"}]}'), Status.VERIFIED, Status.FAILED),
    ],
    ids=["not-ready", "models-error", "malformed", "wrong-shape"],
)
def test_probe_ovms_fails_closed(
    ovms: dict[str, Any], ready: int, models: tuple[int, bytes], daemon: Status, inventory: Status
) -> None:
    ovms.update(ready=ready, models=models)
    provider, per_model = probe_ovms(ovms["spec"], {"a": OV_MODEL})
    assert [c.status for c in provider] == [daemon]
    assert per_model["a"].status is inventory


def test_probe_ovms_stopped() -> None:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    provider, models = probe_ovms({**OPENVINO, "port": port}, {"a": OV_MODEL})
    assert [c.status for c in provider] == [Status.FAILED]
    assert models["a"].status is Status.UNVERIFIED


def test_ovms_model_version_requires_the_model_to_be_served(ovms: dict[str, Any]) -> None:
    ovms["models"] = _served(OV_MODEL)
    assert (
        ovms_model_version(ovms["spec"], OV_MODEL) == f"{OV_MODEL}@ovms (no build digest reported)"
    )
    with pytest.raises(LookupError, match="not served"):
        ovms_model_version(ovms["spec"], "OpenVINO/other")
    ovms["models"] = (500, b"")
    with pytest.raises(LookupError, match="HTTP 500"):
        ovms_model_version(ovms["spec"], OV_MODEL)


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
    _proc(tmp_path, tcp, tcp6)
    assert probe_listeners(11434, fake_host(tmp_path)).status is expected


def test_probe_listeners_ignores_non_listening_sockets(tmp_path: Path) -> None:
    proc = _proc(tmp_path, [f"{V4_LOOPBACK}:2CAA"], [])
    tcp = proc / "net" / "tcp"
    tcp.write_text(
        tcp.read_text() + f"   9: {V4_ANY}:2CAA 0100007F:DC62 01 00000000:00000000\n",
        encoding="ascii",
    )
    assert probe_listeners(11434, fake_host(tmp_path)).status is Status.VERIFIED


def _listing_host(tmp_path: Path, platform: str, outputs: dict[str, tuple[int, str]]) -> Host:
    """A host whose listener tool answers from `outputs`, keyed by the argv after the tool."""

    def run(argv: list[str], env: Mapping[str, str]) -> tuple[int, str]:
        assert env.keys() <= {"HOME", "LANG", "PATH", "SYSTEMROOT"}
        return outputs[" ".join(argv[1:])]

    return dataclasses.replace(fake_host(tmp_path), platform=platform, run=run)


LSOF = "-nP -iTCP:11434 -sTCP:LISTEN -Fn"


@pytest.mark.parametrize(
    ("output", "code", "expected"),
    [
        ("p812\nf6\nn127.0.0.1:11434\n", 0, Status.VERIFIED),
        ("p812\nf6\nn[::1]:11434\nf7\nn127.0.0.1:11434\n", 0, Status.VERIFIED),
        ("p812\nf6\nn*:11434\n", 0, Status.FAILED),
        ("p812\nf6\nn192.168.1.4:11434\n", 0, Status.FAILED),
        ("", 1, Status.UNVERIFIED),  # lsof found nothing
        ("", 2, Status.UNVERIFIED),
        ("p812\nnnot-an-address\n", 0, Status.UNVERIFIED),
    ],
    ids=["v4-loopback", "both-loopbacks", "wildcard", "lan", "none", "lsof-error", "garbled"],
)
def test_probe_listeners_on_macos_reads_lsof(
    tmp_path: Path, output: str, code: int, expected: Status
) -> None:
    host = _listing_host(tmp_path, "darwin", {LSOF: (code, output)})
    assert probe_listeners(11434, host).status is expected


NETSTAT_HEAD = "\nActive Connections\n\n  Proto  Local Address  Foreign Address  State  PID\n"


@pytest.mark.parametrize(
    ("v4", "v6", "expected"),
    [
        (["TCP    127.0.0.1:11434    0.0.0.0:0    LISTENING    40"], [], Status.VERIFIED),
        ([], ["TCP    [::1]:11434    [::]:0    ABHÖREN    40"], Status.VERIFIED),  # localized
        (["TCP    0.0.0.0:11434    0.0.0.0:0    LISTENING    40"], [], Status.FAILED),
        (
            ["TCP    127.0.0.1:11434    0.0.0.0:0    LISTENING    40"],
            ["TCP    [::]:11434    [::]:0    LISTENING    40"],
            Status.FAILED,
        ),
        (["TCP    0.0.0.0:11434    10.0.0.2:5555    ESTABLISHED    40"], [], Status.UNVERIFIED),
        (["TCP    0.0.0.0:8080    0.0.0.0:0    LISTENING    40"], [], Status.UNVERIFIED),
    ],
    ids=["v4-loopback", "v6-loopback-localized", "wildcard", "v6-wildcard", "not-listening",
         "other-port"],
)  # fmt: skip
def test_probe_listeners_on_windows_reads_netstat(
    tmp_path: Path, v4: list[str], v6: list[str], expected: Status
) -> None:
    outputs = {
        "-ano -p TCP": (0, NETSTAT_HEAD + "\n".join(v4)),
        "-ano -p TCPv6": (0, NETSTAT_HEAD + "\n".join(v6)),
    }
    assert probe_listeners(11434, _listing_host(tmp_path, "win32", outputs)).status is expected


def test_probe_listeners_without_a_tool_or_platform_is_unverified(tmp_path: Path) -> None:
    missing = dataclasses.replace(fake_host(tmp_path, found=False), platform="darwin")
    assert "lsof not found" in probe_listeners(11434, missing).detail
    other = dataclasses.replace(fake_host(tmp_path), platform="sunos5")
    assert probe_listeners(11434, other).status is Status.UNVERIFIED


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
            "llamacpp_local": [Check("daemon", ok, ""), Check("listener_binding", ok, "")],
        },
        models={"qwen3_5_9b": [Check("model_inventory", ok, "")]},
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
        "proposal_a": "claude_opus_5_5_high_cli",
        "proposal_b": "gemini_4_argon_medium",
    }


def test_a1_blocked_when_one_cloud_agent_unavailable() -> None:
    report = ready_report()
    _downgrade(report.providers["google_ai_pro"], "executable")
    verdict = evaluate_stage("A1", report)
    assert not verdict.eligible
    assert "proposal_b: gemini_4_argon_medium.executable UNVERIFIED" in verdict.reasons


def test_a3r_falls_back_in_declared_order() -> None:
    report = ready_report()
    _downgrade(report.providers["google_ai_pro"], "executable")
    verdict = evaluate_stage("A3R", report)
    assert verdict.eligible and verdict.backends == {"synthesis_review": "claude_cli_default"}


def test_a_model_missing_from_the_local_inventory_blocks_build() -> None:
    report = ready_report()
    _downgrade(report.models["qwen3_5_9b"], "model_inventory", Status.FAILED)
    verdict = evaluate_stage("E1", report)
    assert not verdict.eligible
    assert "implementation_patch: qwen3_5_9b.model_inventory FAILED" in verdict.reasons


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
    [("E4", "stage disabled"), ("BENCH", "stage disabled"), ("G", "unknown or non-model")],
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


# ── While an agent has no model ──────────────────────────────────────────────


def gap_repo(tmp_path: Path) -> Path:
    """A copy of the repository with one more spec that no `agents` entry owns."""
    for name in ("pipeline", "policy", "schemas", "agents"):
        shutil.copytree(ROOT / name, tmp_path / name)
    text = (ROOT / "agents" / "pipeline-learning-agent.md").read_text(encoding="utf-8")
    (tmp_path / "agents" / "pipeline-extra-helper-agent.md").write_text(text, encoding="utf-8")
    return tmp_path


def test_the_gap_tolerant_preflight_probes_where_the_default_one_stops(tmp_path: Path) -> None:
    repo = gap_repo(tmp_path)
    host = Host(environ={}, home=tmp_path, which=lambda name: None)
    gap = "registry R20: agents/pipeline-extra-helper-agent.md is not the spec of any agents entry"
    assert run_preflight(repo, host).static_errors == [gap]  # the gate has not moved
    report = run_preflight_tolerating_gaps(repo, host)
    assert report.static_errors == []
    assert report.registry["models"] and report.providers and report.isolation
    assert not evaluate_stage("A1", run_preflight(repo, host)).eligible


def test_the_gap_tolerant_preflight_drops_the_gap_messages_and_nothing_else(
    tmp_path: Path,
) -> None:
    repo = gap_repo(tmp_path)
    release = repo / "agents" / "pipeline-release-agent.md"  # an owned spec that loses its brief
    text = release.read_text(encoding="utf-8")
    release.write_text(text.replace(BEGIN, "").replace(END, ""), encoding="utf-8")

    def refuse(*args: object) -> Any:
        raise AssertionError("host probed despite a static violation")

    host = Host(environ={}, home=tmp_path, which=refuse, run=refuse)
    errors = run_preflight_tolerating_gaps(repo, host).static_errors
    assert len(errors) == 1 and errors[0].startswith("registry R21"), errors


def test_two_entries_sharing_a_spec_is_not_tolerated(tmp_path: Path) -> None:
    repo = gap_repo(tmp_path)
    path = repo / "policy" / "model_registry.yaml"
    text = path.read_text(encoding="utf-8")
    old = "spec: agents/pipeline-release-agent.md"
    assert text.count(old) == 1
    path.write_text(text.replace(old, "spec: agents/pipeline-learning-agent.md"), encoding="utf-8")
    host = Host(environ={}, home=tmp_path, which=lambda name: None)
    errors = run_preflight_tolerating_gaps(repo, host).static_errors
    assert any("is also agents." in e for e in errors), errors


def test_the_gap_tolerant_preflight_is_the_default_one_when_there_is_no_gap(
    tmp_path: Path,
) -> None:
    host = Host(environ={}, home=tmp_path, which=lambda name: None)
    tolerant = run_preflight_tolerating_gaps(ROOT, host)
    default = run_preflight(ROOT, host)
    assert tolerant.static_errors == default.static_errors == []
    assert tolerant.providers.keys() == default.providers.keys()


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


def test_a_launchable_server_that_is_down_is_started_on_demand_not_failed(tmp_path: Path) -> None:
    frozen = copy.deepcopy(REGISTRY)
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]  # closed again: nothing listens there
    decision = frozen["providers"]["llamacpp_decision"]
    decision["readiness"]["port"] = port
    del frozen["providers"]["llamacpp_local"]["launch"]
    frozen["providers"]["llamacpp_local"]["readiness"]["port"] = port
    report = run_preflight(ROOT, Host(environ={}, home=tmp_path, which=lambda name: None), frozen)
    daemon = report.providers["llamacpp_decision"][0]
    assert (daemon.status, "on demand" in daemon.detail) == (Status.UNVERIFIED, True)
    assert report.providers["llamacpp_local"][0].status is Status.FAILED  # no launch: as before
