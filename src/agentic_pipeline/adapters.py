"""Backend adapters. Each runs one granted operation and declares what it enforces.

PiQwenAdapter runs the Pi coding agent inside the bwrap sandbox (sandbox.py).
Pi's only model is a `broker` provider pointing at the in-sandbox shim, which
relays to the host-side InferenceBroker, which alone talks to Ollama. Pi runs
offline, ignores project-local config in its inputs, saves no session, loads
no extensions/skills/MCP, and gets file tools only (no shell).
"""

from __future__ import annotations

import json
import shutil
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agentic_pipeline.broker import BrokerPolicy, InferenceBroker, Upstream, ollama_upstream
from agentic_pipeline.dispatch import AdapterResult, DispatchGrant, PreparedCall
from agentic_pipeline.readiness import ollama_model_digest
from agentic_pipeline.sandbox import (
    HOME,
    INPUTS,
    WORK,
    SandboxLimits,
    SandboxResult,
    SandboxSpec,
    SandboxUnavailable,
    run_sandboxed,
)
from agentic_pipeline.sandbox_entry import SHIM_URL

PI_PACKAGE_NAME = "@earendil-works/pi-coding-agent"
PI_MOUNT = "/opt/pi"
PI_CONFIG_DIR = f"{HOME}/.pi/agent"
PI_PROVIDER = "broker"
PI_TOOLS = ("read", "ls", "find", "grep", "edit", "write")
PI_FLAGS = (
    "--print",
    "--offline",
    "--no-session",
    "--no-approve",
    "--no-extensions",
    "--no-skills",
    "--no-prompt-templates",
    "--no-themes",
    "--no-context-files",
    "--no-mcp",
)
PI_ENV = {
    "PI_CODING_AGENT_DIR": PI_CONFIG_DIR,
    "PI_OFFLINE": "1",
    "PI_SKIP_VERSION_CHECK": "1",
    "PI_TELEMETRY": "0",
}

type SandboxRunner = Callable[[SandboxSpec, InferenceBroker], SandboxResult]


def find_pi_package(which: Callable[[str], str | None] = shutil.which) -> Path:
    """Locate the installed Pi package from the `pi` launcher; verify it is the expected one."""
    launcher = which("pi")
    if launcher is None:
        raise LookupError("pi not found on PATH")
    for parent in Path(launcher).resolve().parents:
        manifest = parent / "package.json"
        if manifest.is_file():
            meta = json.loads(manifest.read_text(encoding="utf-8"))
            if meta.get("name") == PI_PACKAGE_NAME:
                return parent
            break
    raise LookupError(f"{launcher} is not part of {PI_PACKAGE_NAME}")


def default_prompt(grant: DispatchGrant) -> str:
    inputs = "\n".join(f"- {INPUTS}/{aid}" for aid in sorted(grant.inputs)) or "- (none)"
    return (
        f"You are the {grant.role} for pipeline stage {grant.stage}, "
        f"operation {grant.operation_id}.\n"
        f"Read-only inputs:\n{inputs}\n"
        f"Write every output file under {WORK} (the current directory). "
        "Nothing outside it persists, and there is no network access."
    )


@dataclass(slots=True)
class PiQwenAdapter:
    pi_package: Path
    model_id: str
    model_version: str
    upstream: Upstream
    prompt_builder: Callable[[DispatchGrant], str] = default_prompt
    broker_policy: Callable[[str], BrokerPolicy] = BrokerPolicy
    limits: SandboxLimits = field(default_factory=SandboxLimits)
    timeout: float = 1800.0
    run: SandboxRunner = run_sandboxed
    enforced_isolation: frozenset[str] = frozenset(
        {
            "sandbox_enforcement",
            "protected_paths_read_only",
            "network_disabled",
            "broker_mediated_inference",
        }
    )

    @classmethod
    def from_registry(cls, registry: Mapping[str, Any], **overrides: Any) -> PiQwenAdapter:
        """Build from the qwen_local readiness spec, resolving Pi and the model digest."""
        spec = registry["providers"]["qwen_local"]["readiness"]
        return cls(
            pi_package=overrides.pop("pi_package", None) or find_pi_package(),
            model_id=spec["model_id"],
            model_version=ollama_model_digest(spec),
            upstream=ollama_upstream(spec["host"], spec["port"], timeout=600.0),
            **overrides,
        )

    @property
    def pi_version(self) -> str:
        meta = json.loads((self.pi_package / "package.json").read_text(encoding="utf-8"))
        return str(meta.get("version", "unknown"))

    def prepare(self, grant: DispatchGrant) -> PreparedCall:
        return PreparedCall(
            model=self.model_id,
            model_version=self.model_version,
            access_method=f"local_inference via pi {self.pi_version} in bwrap through broker",
            prompt=self.prompt_builder(grant).encode("utf-8"),
        )

    def models_config(self) -> dict[str, Any]:
        return {
            "providers": {
                PI_PROVIDER: {
                    "baseUrl": SHIM_URL,
                    "api": "openai-completions",
                    "apiKey": "brokered",  # placeholder; the broker holds no credential either
                    "models": [{"id": self.model_id}],
                }
            }
        }

    def sandbox_spec(self, grant: DispatchGrant, call: PreparedCall, config: Path) -> SandboxSpec:
        argv = [
            "node",
            f"{PI_MOUNT}/dist/bundle/cli.js",
            *PI_FLAGS,
            "--provider", PI_PROVIDER,
            "--model", self.model_id,
            "--tools", ",".join(PI_TOOLS),
            call.prompt.decode("utf-8"),
        ]  # fmt: skip
        return SandboxSpec(
            argv=argv,
            write_path=grant.write_path,
            inputs=dict(grant.inputs),
            ro_binds={PI_MOUNT: self.pi_package, f"{PI_CONFIG_DIR}/models.json": config},
            env=PI_ENV,
            timeout=self.timeout,
            limits=self.limits,
        )

    def invoke(self, grant: DispatchGrant, call: PreparedCall) -> AdapterResult:
        broker = InferenceBroker(self.broker_policy(self.model_id), self.upstream)
        with tempfile.TemporaryDirectory(prefix="agentic-pi-") as tmp:
            config = Path(tmp) / "models.json"
            config.write_text(json.dumps(self.models_config()), encoding="utf-8")
            try:
                result = self.run(self.sandbox_spec(grant, call, config), broker)
            except SandboxUnavailable as exc:
                return AdapterResult("error", f"sandbox unavailable: {exc}")
        stats = result.broker
        detail = f"rounds={stats.rounds} rejected={len(stats.rejected)} exit={result.returncode}"
        if result.timed_out:
            return AdapterResult("error", f"timed out; {detail}")
        if stats.protocol_error:
            return AdapterResult("error", f"broker protocol error: {stats.protocol_error}")
        if result.returncode != 0:
            tail = result.stderr.strip().splitlines()[-1:] or [""]
            return AdapterResult("error", f"{detail}; {tail[0][:200]}")
        return AdapterResult("ok", detail)
