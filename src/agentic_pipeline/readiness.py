"""Read-only runtime readiness preflight.

Probes the host for what `policy/model_registry.yaml` requires before a model
stage may be dispatched. Probes never write files, change settings, log in, or
start services; anything that cannot be proven read-only is reported UNVERIFIED,
and only VERIFIED counts toward stage eligibility.
"""

from __future__ import annotations

import http.client
import ipaddress
import json
import os
import re
import shutil
import subprocess
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from agentic_pipeline.broker import BrokerPolicy, InferenceBroker
from agentic_pipeline.graph import REPO_ROOT, load_yaml
from agentic_pipeline.registry import op_backends, operations
from agentic_pipeline.sandbox import (
    ENTRY,
    SandboxResult,
    SandboxSpec,
    SandboxUnavailable,
    run_sandboxed,
)
from agentic_pipeline.validation import validate_repo

TCP_LISTEN = "0A"
PROBE_TIMEOUT_SECONDS = 5.0


class Status(StrEnum):
    VERIFIED = "VERIFIED"
    UNVERIFIED = "UNVERIFIED"
    FAILED = "FAILED"


@dataclass(frozen=True, slots=True)
class Check:
    name: str
    status: Status
    detail: str


type RunFn = Callable[[list[str], Mapping[str, str]], tuple[int, str]]


def _run(argv: list[str], env: Mapping[str, str]) -> tuple[int, str]:
    try:
        proc = subprocess.run(  # noqa: S603 - argv[0] is an absolute path from shutil.which
            argv,
            env=dict(env),
            capture_output=True,
            text=True,
            timeout=PROBE_TIMEOUT_SECONDS,
            check=False,
            stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return -1, str(exc)
    return proc.returncode, proc.stdout


@dataclass(frozen=True, slots=True)
class Host:
    """Everything a probe reads from the host, injectable for tests."""

    environ: Mapping[str, str]
    home: Path
    proc_root: Path = Path("/proc")
    which: Callable[[str], str | None] = shutil.which
    run: RunFn = _run
    run_sandbox: Callable[[SandboxSpec, InferenceBroker, str], SandboxResult] = run_sandboxed

    @classmethod
    def real(cls) -> Host:
        return cls(environ=dict(os.environ), home=Path.home())

    def child_env(self) -> dict[str, str]:
        """Allowlisted environment for child processes (registry child_process_env)."""
        env = {"HOME": str(self.home), "LANG": "C.UTF-8"}
        if "PATH" in self.environ:
            env["PATH"] = self.environ["PATH"]
        return env


# ── Probes ───────────────────────────────────────────────────────────────────


def probe_credential_env(denylist: list[str], environ: Mapping[str, str]) -> Check:
    present = sorted(name for name in denylist if name in environ)
    if present:
        return Check("credential_env_absent", Status.FAILED, f"prohibited variables set: {present}")
    return Check(
        "credential_env_absent",
        Status.VERIFIED,
        f"none of {len(denylist)} prohibited variables set",
    )


def probe_executable(spec: Mapping[str, Any], host: Host) -> Check:
    name = spec["executable"]
    path = host.which(name)
    if path is None:
        return Check("executable", Status.FAILED, f"{name} not found on PATH")
    code, out = host.run([path, "--version"], host.child_env())
    if code != 0:
        return Check("executable", Status.FAILED, f"{name} --version exited {code}")
    match = re.match(spec["version_pattern"], out.strip())
    if match is None:
        return Check("executable", Status.FAILED, f"unrecognized version output {out.strip()!r}")
    found = tuple(int(g) for g in match.groups())
    wanted = tuple(spec["min_version"])
    shown = ".".join(map(str, found))
    if found < wanted:
        minimum = ".".join(map(str, wanted))
        return Check("executable", Status.FAILED, f"{name} {shown} < {minimum}")
    return Check("executable", Status.VERIFIED, f"{name} {shown} at {path}")


def probe_subscription_auth() -> Check:
    return Check(
        "subscription_auth",
        Status.UNVERIFIED,
        "no read-only, non-mutating auth verifier is available",
    )


def probe_settings(spec: Mapping[str, Any], host: Host) -> Check:
    rel = spec["settings_path"].removeprefix("~/")
    path = host.home / rel
    key, wanted = spec["settings_key"], spec["settings_required_value"]
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return Check("overage_policy", Status.UNVERIFIED, f"{spec['settings_path']} not found")
    except (OSError, ValueError) as exc:
        return Check("overage_policy", Status.UNVERIFIED, f"unreadable settings: {exc}")
    if not isinstance(doc, dict) or key not in doc:
        return Check("overage_policy", Status.UNVERIFIED, f"{key} not set; default unconfirmed")
    if doc[key] != wanted:
        return Check("overage_policy", Status.FAILED, f"{key} is {doc[key]!r}, need {wanted!r}")
    return Check("overage_policy", Status.VERIFIED, f"{key} is {wanted!r}")


def _ollama_tags(spec: Mapping[str, Any]) -> tuple[int, bytes]:
    conn = http.client.HTTPConnection(spec["host"], spec["port"], timeout=spec["timeout_seconds"])
    try:
        conn.request("GET", "/api/tags")
        resp = conn.getresponse()
        return resp.status, resp.read()
    finally:
        conn.close()


def _tagged_models(body: bytes) -> dict[str, str]:
    """name -> digest from an /api/tags body; raises on malformed input."""
    return {m["name"]: m.get("digest", "") for m in json.loads(body)["models"]}


def probe_ollama(spec: Mapping[str, Any]) -> list[Check]:
    host, port, model = spec["host"], spec["port"], spec["model_id"]
    try:
        status, body = _ollama_tags(spec)
    except OSError as exc:
        daemon = Check("daemon", Status.FAILED, f"{host}:{port} unreachable: {exc}")
        return [daemon, Check("model_inventory", Status.UNVERIFIED, "daemon unreachable")]
    if status != 200:
        daemon = Check("daemon", Status.FAILED, f"GET /api/tags returned HTTP {status}")
        return [daemon, Check("model_inventory", Status.UNVERIFIED, "daemon unhealthy")]
    daemon = Check("daemon", Status.VERIFIED, f"ollama responding on {host}:{port}")
    try:
        names = _tagged_models(body)
    except (ValueError, KeyError, TypeError):
        return [daemon, Check("model_inventory", Status.FAILED, "malformed /api/tags response")]
    if model not in names:
        return [daemon, Check("model_inventory", Status.FAILED, f"{model} not pulled")]
    return [daemon, Check("model_inventory", Status.VERIFIED, f"{model} present")]


def ollama_model_digest(spec: Mapping[str, Any]) -> str:
    """Resolve the exact local model build, as runtime evidence for provenance."""
    status, body = _ollama_tags(spec)
    if status != 200:
        raise LookupError(f"GET /api/tags returned HTTP {status}")
    try:
        digest = _tagged_models(body).get(spec["model_id"])
    except (ValueError, KeyError, TypeError) as exc:
        raise LookupError("malformed /api/tags response") from exc
    if not digest:
        raise LookupError(f"{spec['model_id']} not pulled or has no digest")
    return f"{spec['model_id']}@sha256:{digest}"


def _decode_proc_addr(hex_addr: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    # /proc/net/tcp{,6} stores each 32-bit word in host (little-endian) byte order.
    raw = b"".join(bytes.fromhex(hex_addr[i : i + 8])[::-1] for i in range(0, len(hex_addr), 8))
    return ipaddress.ip_address(raw)


def _is_loopback(addr: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    mapped = getattr(addr, "ipv4_mapped", None)
    return addr.is_loopback or (mapped is not None and mapped.is_loopback)


def probe_listeners(port: int, proc_root: Path) -> Check:
    listeners: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
    for table in ("tcp", "tcp6"):
        try:
            lines = (proc_root / "net" / table).read_text(encoding="ascii").splitlines()[1:]
        except OSError:
            continue
        for line in lines:
            fields = line.split()
            if len(fields) < 4 or fields[3] != TCP_LISTEN:
                continue
            addr_hex, port_hex = fields[1].split(":")
            if int(port_hex, 16) == port:
                listeners.append(_decode_proc_addr(addr_hex))
    exposed = sorted(str(a) for a in listeners if not _is_loopback(a))
    if exposed:
        return Check("listener_binding", Status.FAILED, f"port {port} bound on {exposed}")
    if not listeners:
        return Check("listener_binding", Status.UNVERIFIED, f"no listener on port {port} found")
    bound = sorted(str(a) for a in listeners)
    return Check("listener_binding", Status.VERIFIED, f"port {port} loopback-only {bound}")


SANDBOX_CHECKS = (
    "sandbox_enforcement",
    "protected_paths_read_only",
    "network_disabled",
    "broker_mediated_inference",
)
PROBE_MODEL = "probe-model"
PROBE_REPLY = (b"probe-", b"ok")  # two chunks, so streaming relay is exercised
PROBE_INPUT = Path(__file__).resolve().parent  # any existing directory; bound read-only


def _probe_upstream(path: str, body: bytes) -> tuple[int, Mapping[str, str], Iterator[bytes]]:
    return 200, {"Content-Type": "text/plain"}, iter(PROBE_REPLY)


def parse_sandbox_probe(result: SandboxResult) -> dict[str, Check]:
    """Turn the in-sandbox probe report into checks; anything unexpected fails closed."""
    report: dict[str, Any] = {}
    if result.returncode == 0 and not result.timed_out:
        try:
            report = json.loads(result.stdout.strip().splitlines()[-1])
        except (ValueError, IndexError):
            report = {}
    if not isinstance(report, dict) or not report:
        why = "timed out" if result.timed_out else f"exited {result.returncode}"
        tail = result.stderr.strip().splitlines()[-1:] or [""]
        failed = Check("sandbox_enforcement", Status.FAILED, f"self-test {why} {tail[0]}".strip())
        unknown = "sandbox self-test did not complete"
        return {
            "sandbox_enforcement": failed,
            **{n: Check(n, Status.UNVERIFIED, unknown) for n in SANDBOX_CHECKS[1:]},
        }

    def check(name: str, ok: bool, good: str, bad: str) -> Check:
        return Check(name, Status.VERIFIED if ok else Status.FAILED, good if ok else bad)

    contained = (
        report.get("write_root_writable") is True and report.get("host_home_visible") is False
    )
    read_only = report.get("input_writable") is False
    interfaces = report.get("interfaces")
    foreign = report.get("foreign_model") or [0]
    brokered = (
        report.get("brokered") == [200, b"".join(PROBE_REPLY).decode()]
        and foreign[0] == 403
        and result.broker.rounds == 1
    )
    return {
        "sandbox_enforcement": check(
            "sandbox_enforcement",
            contained,
            "bwrap: only /work writable, host home invisible",
            f"bwrap: write_root={report.get('write_root_writable')}, "
            f"host_home_visible={report.get('host_home_visible')}",
        ),
        "protected_paths_read_only": check(
            "protected_paths_read_only",
            read_only,
            "inputs mounted read-only",
            "inputs writable inside sandbox",
        ),
        "network_disabled": check(
            "network_disabled",
            interfaces == ["lo"],
            "only private loopback inside sandbox",
            f"interfaces inside sandbox: {interfaces}",
        ),
        "broker_mediated_inference": check(
            "broker_mediated_inference",
            brokered,
            "shim -> pipes -> broker round trip; foreign model refused",
            f"brokered={report.get('brokered')}, foreign_model={foreign}",
        ),
    }


def probe_sandbox(host: Host) -> dict[str, Check]:
    """Run the real worker sandbox configuration with a canned, offline broker upstream."""
    bwrap = host.which("bwrap")
    if bwrap is None:
        return {n: Check(n, Status.UNVERIFIED, "bwrap not found on PATH") for n in SANDBOX_CHECKS}
    spec = SandboxSpec(
        argv=["python3", "-I", ENTRY, "--probe", str(host.home), PROBE_MODEL],
        inputs={"probe": PROBE_INPUT},
        timeout=PROBE_TIMEOUT_SECONDS * 6,
    )
    broker = InferenceBroker(BrokerPolicy(PROBE_MODEL), _probe_upstream)
    try:
        result = host.run_sandbox(spec, broker, bwrap)
    except SandboxUnavailable as exc:
        failed = Check("sandbox_enforcement", Status.FAILED, f"sandbox did not start: {exc}")
        unknown = "sandbox self-test did not complete"
        return {
            "sandbox_enforcement": failed,
            **{n: Check(n, Status.UNVERIFIED, unknown) for n in SANDBOX_CHECKS[1:]},
        }
    return parse_sandbox_probe(result)


# ── Report and stage eligibility ─────────────────────────────────────────────


NOT_YET_IMPLEMENTED = {
    "session_separation": "no session-isolation mechanism implemented for cloud tools",
}


@dataclass(slots=True)
class Report:
    static_errors: list[str]
    global_checks: list[Check] = field(default_factory=list)
    providers: dict[str, list[Check]] = field(default_factory=dict)
    isolation: dict[str, Check] = field(default_factory=dict)
    registry: Mapping[str, Any] = field(default_factory=dict)

    def provider_blockers(self, name: str) -> list[str]:
        """Checks keeping `name` from dispatch; empty means ready."""
        if name not in self.providers:
            return [f"{name}: no readiness probe declared"]
        return [
            f"{name}.{c.name} {c.status}"
            for c in [*self.global_checks, *self.providers[name]]
            if c.status is not Status.VERIFIED
        ]


@dataclass(frozen=True, slots=True)
class StageVerdict:
    stage: str
    eligible: bool
    backends: dict[str, str]
    reasons: list[str]


def run_preflight(root: Path = REPO_ROOT, host: Host | None = None) -> Report:
    errors = validate_repo(root)
    if errors:
        return Report(static_errors=errors)
    host = host or Host.real()
    registry = load_yaml(root / "policy" / "model_registry.yaml")
    report = Report(static_errors=[], registry=registry)
    report.global_checks.append(
        probe_credential_env(registry["policy"]["prohibited_credential_env_vars"], host.environ)
    )
    for name, provider in registry["providers"].items():
        spec = provider.get("readiness")
        if spec is None:
            continue
        if provider["access_mode"] == "subscription_tool":
            checks = [probe_executable(spec, host), probe_subscription_auth()]
            if "settings_path" in spec:
                checks.append(probe_settings(spec, host))
        else:
            checks = [*probe_ollama(spec), probe_listeners(spec["port"], host.proc_root)]
        report.providers[name] = checks

    report.isolation = probe_sandbox(host)
    report.isolation["proposal_path_layout"] = Check(
        "proposal_path_layout", Status.VERIFIED, "workspace roots distinct and non-nested (R12)"
    )
    report.isolation["sealed_input_verification"] = Check(
        "sealed_input_verification",
        Status.VERIFIED,
        "enforced per dispatch: inputs re-hashed against published stage manifests",
    )
    for name, why in NOT_YET_IMPLEMENTED.items():
        report.isolation[name] = Check(name, Status.UNVERIFIED, why)
    return report


def evaluate_stage(stage: str, report: Report) -> StageVerdict:
    if report.static_errors:
        return StageVerdict(stage, False, {}, ["static repository validation failed"])
    stage_reqs = report.registry["stage_requirements"]
    spec = stage_reqs.get(stage)
    if spec is None:
        return StageVerdict(stage, False, {}, [f"unknown or non-model stage {stage!r}"])
    if spec["mode"] == "disabled":
        return StageVerdict(stage, False, {}, [f"stage disabled: {spec['reason']}"])

    reasons: list[str] = []
    selected: dict[str, str] = {}
    profiles = report.registry["isolation_profiles"]
    for _, op in operations({stage: spec}):
        for check_name in profiles[op["isolation_profile"]]:
            check = report.isolation[check_name]
            if check.status is not Status.VERIFIED:
                reasons.append(f"{op['id']}: isolation {check_name} {check.status}")
        candidates = op_backends(report.registry, stage, op)
        blockers = {b: report.provider_blockers(b) for b in candidates}
        ready = next((b for b in candidates if not blockers[b]), None)
        if ready is None:
            reasons.extend(f"{op['id']}: {r}" for b in candidates for r in blockers[b])
        else:
            selected[op["id"]] = ready
    return StageVerdict(stage, not reasons, selected, reasons)
