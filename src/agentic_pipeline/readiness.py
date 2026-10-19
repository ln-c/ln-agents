"""Read-only runtime readiness preflight.

Probes the host for what `policy/model_registry.yaml` requires before a model
stage may be dispatched. Probes never write files, change settings, log in, or
start services; anything that cannot be proven read-only is reported UNVERIFIED,
and only VERIFIED counts toward stage eligibility.

For subscription-tool providers the pipeline checks only that the agent is available (its
executable is on PATH at an accepted version) and that no prohibited credential variable is
set. Which account or plan the tool is logged into, and its billing settings, are the
user's responsibility: the pipeline neither inspects nor depends on any provider's login
state. A call that fails on auth or quota is reported by the adapter at run time.
"""

from __future__ import annotations

import hashlib
import http.client
import ipaddress
import json
import os
import re
import shutil
import subprocess
import sys
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
    platform: str = sys.platform  # selects the listener probe: linux, darwin, win32
    which: Callable[[str], str | None] = shutil.which
    run: RunFn = _run
    run_sandbox: Callable[[SandboxSpec, InferenceBroker, str], SandboxResult] = run_sandboxed

    @classmethod
    def real(cls) -> Host:
        return cls(environ=dict(os.environ), home=Path.home())

    def child_env(self) -> dict[str, str]:
        """Allowlisted environment for child processes (registry child_process_env)."""
        env = {"HOME": str(self.home), "LANG": "C.UTF-8"}
        # Windows programs such as netstat need SYSTEMROOT to start.
        for name in ("PATH", "SYSTEMROOT"):
            if name in self.environ:
                env[name] = self.environ[name]
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


def resolve_executable(spec: Mapping[str, Any], host: Host) -> tuple[str, str]:
    """(absolute path, version) of the declared executable; LookupError if unusable."""
    name = spec["executable"]
    path = host.which(name)
    if path is None:
        raise LookupError(f"{name} not found on PATH")
    code, out = host.run([path, "--version"], host.child_env())
    if code != 0:
        raise LookupError(f"{name} --version exited {code}")
    match = re.match(spec["version_pattern"], out.strip())
    if match is None:
        raise LookupError(f"unrecognized version output {out.strip()!r}")
    found = tuple(int(g) for g in match.groups())
    shown = ".".join(map(str, found))
    if found < tuple(spec["min_version"]):
        minimum = ".".join(map(str, spec["min_version"]))
        raise LookupError(f"{name} {shown} < {minimum}")
    return path, shown


def probe_executable(spec: Mapping[str, Any], host: Host) -> Check:
    try:
        path, version = resolve_executable(spec, host)
    except LookupError as exc:
        return Check("executable", Status.FAILED, str(exc))
    return Check("executable", Status.VERIFIED, f"{spec['executable']} {version} at {path}")


def _local_get(spec: Mapping[str, Any], path: str) -> tuple[int, bytes]:
    conn = http.client.HTTPConnection(spec["host"], spec["port"], timeout=spec["timeout_seconds"])
    try:
        conn.request("GET", path)
        resp = conn.getresponse()
        return resp.status, resp.read()
    finally:
        conn.close()


LLAMACPP_HEALTH = "/health"  # 200 when ready, 503 while the model loads
LLAMACPP_MODELS = "/v1/models"  # one entry: the model the server was started with (its --alias)
LLAMACPP_PROPS = "/props"  # build_info and model_path


def _llamacpp_served(body: bytes) -> set[str]:
    """Served model ids from an OpenAI-style /v1/models body; raises on malformed input."""
    return {str(m["id"]) for m in json.loads(body)["data"]}


def probe_llamacpp(
    spec: Mapping[str, Any], model_ids: Mapping[str, str]
) -> tuple[list[Check], dict[str, Check]]:
    """(provider checks, per-model inventory checks keyed by catalog id) for a llama-server.

    `model_ids` maps each catalog model id served by this provider to the alias the server was
    started with. A server holds one model, so at most one catalog model can be verified.
    """
    host, port = spec["host"], spec["port"]

    def all_models(status: Status, detail: str) -> dict[str, Check]:
        return {name: Check("model_inventory", status, detail) for name in model_ids}

    try:
        status, _ = _local_get(spec, LLAMACPP_HEALTH)
    except OSError as exc:
        daemon = Check("daemon", Status.FAILED, f"{host}:{port} unreachable: {exc}")
        return [daemon], all_models(Status.UNVERIFIED, "server unreachable")
    if status != 200:
        detail = "still loading its model" if status == 503 else f"returned HTTP {status}"
        daemon = Check("daemon", Status.FAILED, f"GET {LLAMACPP_HEALTH} {detail}")
        return [daemon], all_models(Status.UNVERIFIED, "server not ready")
    daemon = Check("daemon", Status.VERIFIED, f"llama-server ready on {host}:{port}")
    try:
        status, body = _local_get(spec, LLAMACPP_MODELS)
        if status != 200:
            return [daemon], all_models(
                Status.UNVERIFIED, f"GET {LLAMACPP_MODELS} returned {status}"
            )
        served = _llamacpp_served(body)
    except OSError as exc:
        return [daemon], all_models(Status.UNVERIFIED, f"GET {LLAMACPP_MODELS} failed: {exc}")
    except (ValueError, KeyError, TypeError):
        return [daemon], all_models(Status.FAILED, f"malformed {LLAMACPP_MODELS} response")
    return [daemon], {
        name: (
            Check("model_inventory", Status.VERIFIED, f"{alias} served")
            if alias in served
            else Check("model_inventory", Status.FAILED, f"{alias} not served")
        )
        for name, alias in model_ids.items()
    }


_GGUF_DIGESTS: dict[tuple[str, int, int], str] = {}


def _file_sha256(path: Path) -> str:
    """sha256 of a (large) file, remembered per process by path, size and modification time."""
    stat = path.stat()
    key = (str(path), stat.st_size, stat.st_mtime_ns)
    if key not in _GGUF_DIGESTS:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            while chunk := handle.read(16 * 1024 * 1024):
                digest.update(chunk)
        _GGUF_DIGESTS[key] = digest.hexdigest()
    return _GGUF_DIGESTS[key]


def llamacpp_model_version(spec: Mapping[str, Any], model_id: str) -> str:
    """Provenance for the served model: its GGUF's sha256 and the llama.cpp build that runs it.

    The server is on this host, so the file it reports is read and hashed here; if that is not
    possible no version is claimed.
    """
    status, body = _local_get(spec, LLAMACPP_MODELS)
    if status != 200:
        raise LookupError(f"GET {LLAMACPP_MODELS} returned HTTP {status}")
    try:
        served = _llamacpp_served(body)
    except (ValueError, KeyError, TypeError) as exc:
        raise LookupError(f"malformed {LLAMACPP_MODELS} response") from exc
    if model_id not in served:
        raise LookupError(f"{model_id} is not served")
    status, body = _local_get(spec, LLAMACPP_PROPS)
    if status != 200:
        raise LookupError(f"GET {LLAMACPP_PROPS} returned HTTP {status}")
    try:
        props = json.loads(body)
        model_path, build = Path(props["model_path"]), str(props["build_info"])
    except (ValueError, KeyError, TypeError) as exc:
        raise LookupError(f"malformed {LLAMACPP_PROPS} response") from exc
    try:
        digest = _file_sha256(model_path)
    except OSError as exc:
        raise LookupError(f"cannot read the model file {model_path}: {exc}") from exc
    return f"{model_id}@sha256:{digest} (llama.cpp {build})"


def llamacpp_output_modalities(spec: Mapping[str, Any], model_id: str) -> frozenset[str]:
    """What the served model says it outputs (`text`, or `decisions` for a decision model)."""
    status, body = _local_get(spec, LLAMACPP_MODELS)
    if status != 200:
        raise LookupError(f"GET {LLAMACPP_MODELS} returned HTTP {status}")
    try:
        entry = next(m for m in json.loads(body)["data"] if m["id"] == model_id)
        return frozenset(map(str, entry["architecture"]["output_modalities"]))
    except StopIteration:
        raise LookupError(f"{model_id} is not served") from None
    except (ValueError, KeyError, TypeError) as exc:
        raise LookupError(f"malformed {LLAMACPP_MODELS} response") from exc


OVMS_READY = "/v2/health/ready"  # KServe health endpoint
OVMS_MODELS = "/v3/models"  # OpenAI-compatible model list


def _ovms_models(body: bytes) -> set[str]:
    """Served model names from an OpenAI-style /v3/models body; raises on malformed input."""
    return {str(m["id"]) for m in json.loads(body)["data"]}


def probe_ovms(
    spec: Mapping[str, Any], model_ids: Mapping[str, str]
) -> tuple[list[Check], dict[str, Check]]:
    """(provider checks, per-model inventory checks keyed by catalog id) for an OpenVINO server.

    `model_ids` maps each catalog model id served by this provider to the name OVMS serves it
    under. The endpoints follow the OVMS documentation and have not been exercised against a
    live server here; any answer that does not match is a failed or unverified check.
    """
    host, port = spec["host"], spec["port"]

    def all_models(status: Status, detail: str) -> dict[str, Check]:
        return {name: Check("model_inventory", status, detail) for name in model_ids}

    try:
        status, _ = _local_get(spec, OVMS_READY)
    except OSError as exc:
        daemon = Check("daemon", Status.FAILED, f"{host}:{port} unreachable: {exc}")
        return [daemon], all_models(Status.UNVERIFIED, "server unreachable")
    if status != 200:
        daemon = Check("daemon", Status.FAILED, f"GET {OVMS_READY} returned HTTP {status}")
        return [daemon], all_models(Status.UNVERIFIED, "server not ready")
    daemon = Check("daemon", Status.VERIFIED, f"ovms ready on {host}:{port}")
    try:
        status, body = _local_get(spec, OVMS_MODELS)
        if status != 200:
            return [daemon], all_models(Status.UNVERIFIED, f"GET {OVMS_MODELS} returned {status}")
        served = _ovms_models(body)
    except OSError as exc:
        return [daemon], all_models(Status.UNVERIFIED, f"GET {OVMS_MODELS} failed: {exc}")
    except (ValueError, KeyError, TypeError):
        return [daemon], all_models(Status.FAILED, f"malformed {OVMS_MODELS} response")
    return [daemon], {
        name: (
            Check("model_inventory", Status.VERIFIED, f"{served_as} served")
            if served_as in served
            else Check("model_inventory", Status.FAILED, f"{served_as} not served")
        )
        for name, served_as in model_ids.items()
    }


def ovms_model_version(spec: Mapping[str, Any], model_id: str) -> str:
    """Provenance string for a served model; OVMS reports no build digest, so none is pinned."""
    status, body = _local_get(spec, OVMS_MODELS)
    if status != 200:
        raise LookupError(f"GET {OVMS_MODELS} returned HTTP {status}")
    try:
        served = _ovms_models(body)
    except (ValueError, KeyError, TypeError) as exc:
        raise LookupError(f"malformed {OVMS_MODELS} response") from exc
    if model_id not in served:
        raise LookupError(f"{model_id} is not served")
    return f"{model_id}@ovms (no build digest reported)"


def _decode_proc_addr(hex_addr: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    # /proc/net/tcp{,6} stores each 32-bit word in host (little-endian) byte order.
    raw = b"".join(bytes.fromhex(hex_addr[i : i + 8])[::-1] for i in range(0, len(hex_addr), 8))
    return ipaddress.ip_address(raw)


def _is_loopback(addr: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    mapped = getattr(addr, "ipv4_mapped", None)
    return addr.is_loopback or (mapped is not None and mapped.is_loopback)


type Address = ipaddress.IPv4Address | ipaddress.IPv6Address


def _proc_listeners(port: int, proc_root: Path) -> list[Address]:
    """Linux: TCP listeners on `port` from /proc/net/tcp and tcp6."""
    listeners: list[Address] = []
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
    return listeners


def _split_host_port(endpoint: str) -> tuple[Address, int]:
    """`127.0.0.1:8080`, `[::1]:8080`, `*:8080` (all interfaces) or `[fe80::1%5]:8080`."""
    host, _, port = endpoint.rpartition(":")
    host = host.strip("[]").split("%")[0]
    return ipaddress.ip_address("0.0.0.0" if host == "*" else host), int(port)  # noqa: S104


def _lsof_listeners(port: int, host: Host) -> list[Address]:
    """macOS: `lsof -Fn` field output; one `n<address>:<port>` line per listening socket."""
    tool = host.which("lsof")
    if tool is None:
        raise LookupError("lsof not found on PATH")
    argv = [tool, "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-Fn"]
    code, out = host.run(argv, host.child_env())
    if code not in (0, 1):  # 1 means nothing matched
        raise LookupError(f"lsof exited {code}")
    return [_split_host_port(line[1:])[0] for line in out.splitlines() if line.startswith("n")]


def _netstat_listeners(port: int, host: Host) -> list[Address]:
    """Windows: `netstat -ano`. A listening row has the foreign address `0.0.0.0:0` or `[::]:0`;
    the state column is localized, so it is not read."""
    tool = host.which("netstat")
    if tool is None:
        raise LookupError("netstat not found on PATH")
    code, out = host.run([tool, "-ano", "-p", "TCP"], host.child_env())
    code6, out6 = host.run([tool, "-ano", "-p", "TCPv6"], host.child_env())
    if code != 0 or code6 != 0:
        raise LookupError(f"netstat exited {code or code6}")
    listeners: list[Address] = []
    for line in (out + "\n" + out6).splitlines():
        fields = line.split()
        if len(fields) < 3 or fields[0] != "TCP" or fields[2] not in ("0.0.0.0:0", "[::]:0"):
            continue
        address, bound = _split_host_port(fields[1])
        if bound == port:
            listeners.append(address)
    return listeners


def probe_listeners(port: int, host: Host) -> Check:
    """Is every listener on `port` bound to loopback only? Read per OS; unreadable is UNVERIFIED."""
    try:
        if host.platform.startswith("linux"):
            listeners = _proc_listeners(port, host.proc_root)
        elif host.platform == "darwin":
            listeners = _lsof_listeners(port, host)
        elif host.platform == "win32":
            listeners = _netstat_listeners(port, host)
        else:
            return Check("listener_binding", Status.UNVERIFIED, f"no probe for {host.platform}")
    except (LookupError, ValueError) as exc:
        return Check("listener_binding", Status.UNVERIFIED, f"listener table unreadable: {exc}")
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


# Enforced per invocation by adapters, which must declare them (dispatch.py checks).
ADAPTER_ENFORCED = {
    "session_separation": "enforced per invocation by cloud adapters: fresh process, "
    "private config dir, no session persistence",
}


@dataclass(slots=True)
class Report:
    static_errors: list[str]
    global_checks: list[Check] = field(default_factory=list)
    providers: dict[str, list[Check]] = field(default_factory=dict)
    models: dict[str, list[Check]] = field(default_factory=dict)  # checks specific to one model
    isolation: dict[str, Check] = field(default_factory=dict)
    registry: Mapping[str, Any] = field(default_factory=dict)

    def backend_blockers(self, backend: str) -> list[str]:
        """Checks keeping a model backend from dispatch; empty means ready."""
        model = (self.registry.get("models") or {}).get(backend)
        provider = model["provider"] if model else backend
        if provider not in self.providers:
            return [f"{backend}: no readiness probe declared"]
        return [
            f"{backend}.{c.name} {c.status}"
            for c in [*self.global_checks, *self.providers[provider], *self.models.get(backend, ())]
            if c.status is not Status.VERIFIED
        ]


@dataclass(frozen=True, slots=True)
class StageVerdict:
    stage: str
    eligible: bool
    backends: dict[str, str]
    reasons: list[str]


def run_preflight(
    root: Path = REPO_ROOT,
    host: Host | None = None,
    registry: Mapping[str, Any] | None = None,
    policy_root: Path | None = None,
) -> Report:
    """Probe the host. Pass `registry` (and `policy_root`, where the graph and policy are read
    from) to evaluate a run's frozen, drift-checked policy."""
    errors = validate_repo(root, policy_root)
    if errors:
        return Report(static_errors=errors)
    host = host or Host.real()
    if registry is None:
        registry = load_yaml((policy_root or root) / "policy" / "model_registry.yaml")
    report = Report(static_errors=[], registry=registry)
    report.global_checks.append(
        probe_credential_env(registry["policy"]["prohibited_credential_env_vars"], host.environ)
    )
    for name, provider in registry["providers"].items():
        spec = provider.get("readiness")
        if spec is None:
            continue
        if provider["access_mode"] == "subscription_tool":
            report.providers[name] = [probe_executable(spec, host)]
        else:
            tags = {
                m: v["model_id"] for m, v in registry["models"].items() if v["provider"] == name
            }
            probe = {"llamacpp": probe_llamacpp, "ovms": probe_ovms}[spec["protocol"]]
            daemon, inventory = probe(spec, tags)
            if "launch" in provider and daemon[0].status is Status.FAILED:
                why = f"not running; dispatch starts it on demand ({daemon[0].detail})"
                daemon = [Check("daemon", Status.UNVERIFIED, why)]
            report.providers[name] = [*daemon, probe_listeners(spec["port"], host)]
            for model, check in inventory.items():
                report.models[model] = [check]

    report.isolation = probe_sandbox(host)
    report.isolation["proposal_path_layout"] = Check(
        "proposal_path_layout", Status.VERIFIED, "workspace roots distinct and non-nested (R12)"
    )
    report.isolation["sealed_input_verification"] = Check(
        "sealed_input_verification",
        Status.VERIFIED,
        "enforced per dispatch: inputs re-hashed against published stage manifests",
    )
    for name, why in ADAPTER_ENFORCED.items():
        report.isolation[name] = Check(name, Status.VERIFIED, why)
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
        blockers = {b: report.backend_blockers(b) for b in candidates}
        ready = next((b for b in candidates if not blockers[b]), None)
        if ready is None:
            reasons.extend(f"{op['id']}: {r}" for b in candidates for r in blockers[b])
        else:
            selected[op["id"]] = ready
    return StageVerdict(stage, not reasons, selected, reasons)
