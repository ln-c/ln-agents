"""Tests for the Pi + Qwen adapter."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

import pytest

from agentic_pipeline.adapters import (
    PI_CONFIG_DIR,
    PI_MOUNT,
    PiQwenAdapter,
    find_pi_package,
)
from agentic_pipeline.broker import BrokerStats, InferenceBroker
from agentic_pipeline.dispatch import DispatchGrant
from agentic_pipeline.graph import load_yaml
from agentic_pipeline.sandbox import (
    SandboxResult,
    SandboxSpec,
    SandboxUnavailable,
    bwrap_argv,
)
from agentic_pipeline.sandbox_entry import SHIM_URL

ROOT = Path(__file__).resolve().parents[1]
MODEL = "qwen3:8b"


def fake_package(root: Path, name: str = "@earendil-works/pi-coding-agent", js: str = "") -> Path:
    bundle = root / "pkg" / "dist" / "bundle"
    bundle.mkdir(parents=True)
    (root / "pkg" / "package.json").write_text(json.dumps({"name": name, "version": "9.9.9"}))
    (bundle / "cli.js").write_text(js or "process.exit(0);\n")
    return root / "pkg"


def grant(tmp_path: Path, inputs: Mapping[str, Path] | None = None) -> DispatchGrant:
    write_path = tmp_path / "attempt" / "build" / "implementation_patch"
    write_path.mkdir(parents=True)
    return DispatchGrant(
        run_id="run-1",
        stage="E1",
        operation_id="implementation_patch",
        backend="qwen_local",
        invocation_id="inv-1",
        role="patch_author",
        allowed_reads=("contracts_sealed",),
        inputs=dict(inputs or {}),
        write_root="implementation_patch",
        attempt="E1-x",
        write_path=write_path,
    )


def upstream(path: str, body: bytes) -> tuple[int, Mapping[str, str], Iterator[bytes]]:
    reply = {"choices": [{"message": {"content": "patched"}}]}
    return 200, {"Content-Type": "application/json"}, iter([json.dumps(reply).encode()])


def adapter(tmp_path: Path, **kwargs: Any) -> PiQwenAdapter:
    return PiQwenAdapter(
        pi_package=kwargs.pop("pi_package", None) or fake_package(tmp_path),
        model_id=MODEL,
        model_version=f"{MODEL}@sha256:abc",
        upstream=upstream,
        **kwargs,
    )


class Capture:
    """Stands in for run_sandboxed; records the spec and the generated Pi config."""

    def __init__(self, result: SandboxResult | None = None, raises: bool = False) -> None:
        self.result = result or SandboxResult(0, "done", "", False, BrokerStats(rounds=2))
        self.raises = raises
        self.spec: SandboxSpec | None = None
        self.config: dict[str, Any] = {}

    def __call__(self, spec: SandboxSpec, broker: InferenceBroker) -> SandboxResult:
        self.spec = spec
        self.config = json.loads(spec.ro_binds[f"{PI_CONFIG_DIR}/models.json"].read_text())
        if self.raises:
            raise SandboxUnavailable("no user namespaces")
        return self.result


# ── Unit ─────────────────────────────────────────────────────────────────────


def test_prepare_records_resolved_versions_and_prompt(tmp_path: Path) -> None:
    g = grant(tmp_path, {"contracts_sealed": tmp_path})
    call = adapter(tmp_path).prepare(g)
    assert call.model == MODEL and call.model_version == f"{MODEL}@sha256:abc"
    assert "pi 9.9.9" in call.access_method and "bwrap" in call.access_method
    assert b"/inputs/contracts_sealed" in call.prompt and b"patch_author" in call.prompt
    assert call.prompt_hash == adapter(tmp_path / "again").prepare(g).prompt_hash


def test_sandbox_spec_is_locked_down(tmp_path: Path) -> None:
    run = Capture()
    a = adapter(tmp_path, run=run)
    g = grant(tmp_path, {"contracts_sealed": tmp_path})
    assert a.invoke(g, a.prepare(g)).status == "ok"
    spec = run.spec
    assert spec.write_path == g.write_path and spec.inputs == {"contracts_sealed": tmp_path}
    assert spec.ro_binds[PI_MOUNT] == a.pi_package
    for flag in ("--offline", "--no-session", "--no-approve", "--no-extensions", "--no-mcp"):
        assert flag in spec.argv
    tools = spec.argv[spec.argv.index("--tools") + 1].split(",")
    assert "bash" not in tools and "write" in tools
    assert spec.env["PI_OFFLINE"] == "1" and spec.env["PI_TELEMETRY"] == "0"
    provider = run.config["providers"]["broker"]
    assert provider["baseUrl"] == SHIM_URL and provider["models"] == [{"id": MODEL}]
    assert not spec.ro_binds[f"{PI_CONFIG_DIR}/models.json"].exists()  # temp config removed


@pytest.mark.parametrize(
    ("result", "detail"),
    [
        (SandboxResult(0, "", "", True, BrokerStats()), "timed out"),
        (SandboxResult(1, "", "boom", False, BrokerStats()), "boom"),
        (SandboxResult(0, "", "", False, BrokerStats(protocol_error="EOFError")), "protocol"),
    ],
    ids=["timeout", "nonzero-exit", "protocol-error"],
)
def test_failures_map_to_error(tmp_path: Path, result: SandboxResult, detail: str) -> None:
    a = adapter(tmp_path, run=Capture(result))
    g = grant(tmp_path)
    outcome = a.invoke(g, a.prepare(g))
    assert outcome.status == "error" and detail in outcome.detail


def test_sandbox_unavailable_is_error(tmp_path: Path) -> None:
    a = adapter(tmp_path, run=Capture(raises=True))
    g = grant(tmp_path)
    assert a.invoke(g, a.prepare(g)).status == "error"


def test_find_pi_package(tmp_path: Path) -> None:
    pkg = fake_package(tmp_path)
    launcher = tmp_path / "bin" / "pi"
    launcher.parent.mkdir()
    os.symlink(pkg / "dist" / "bundle" / "cli.js", launcher)
    assert find_pi_package(lambda name: str(launcher)) == pkg
    with pytest.raises(LookupError):
        find_pi_package(lambda name: None)


def test_find_pi_package_rejects_other_package(tmp_path: Path) -> None:
    pkg = fake_package(tmp_path, name="not-pi")
    with pytest.raises(LookupError, match="is not part of"):
        find_pi_package(lambda name: str(pkg / "dist" / "bundle" / "cli.js"))


# ── Integration: fake Pi inside the real sandbox ─────────────────────────────


def _sandbox_usable() -> bool:
    bwrap, prlimit, node = (shutil.which(n) for n in ("bwrap", "prlimit", "node"))
    if not (bwrap and prlimit and node):
        return False
    argv = bwrap_argv(bwrap, SandboxSpec(argv=["true"]), 0, 1)
    probe = argv[: argv.index("python3")] + ["/usr/bin/true"]
    return subprocess.run(probe, capture_output=True, check=False).returncode == 0  # noqa: S603


FAKE_PI = r"""
const fs = require("fs");
const cfg = JSON.parse(fs.readFileSync(process.env.PI_CODING_AGENT_DIR + "/models.json"));
const provider = cfg.providers.broker;
(async () => {
  const resp = await fetch(provider.baseUrl + "/chat/completions", {
    method: "POST",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify({model: provider.models[0].id, messages: []}),
  });
  const body = await resp.json();
  fs.writeFileSync("/work/out.md", body.choices[0].message.content);
  fs.writeFileSync("/work/seen.json", JSON.stringify({
    argv: process.argv.slice(2), inputs: fs.readdirSync("/inputs"),
    contract: fs.readFileSync("/inputs/contracts_sealed/c.md", "utf8"),
  }));
})().catch((e) => { console.error(e); process.exit(3); });
"""


@pytest.mark.skipif(not _sandbox_usable(), reason="bwrap, prlimit, or node unavailable")
def test_fake_pi_reaches_model_through_broker(tmp_path: Path) -> None:
    contracts = tmp_path / "contracts"
    contracts.mkdir()
    (contracts / "c.md").write_text("frozen contract")
    pkg = fake_package(tmp_path, js=FAKE_PI)
    a = adapter(tmp_path, pi_package=pkg, timeout=60)
    g = grant(tmp_path, {"contracts_sealed": contracts})

    outcome = a.invoke(g, a.prepare(g))

    assert outcome.status == "ok", outcome.detail
    assert "rounds=1" in outcome.detail
    assert (g.write_path / "out.md").read_text() == "patched"
    seen = json.loads((g.write_path / "seen.json").read_text())
    assert seen["inputs"] == ["contracts_sealed"] and seen["contract"] == "frozen contract"
    assert "--offline" in seen["argv"]


# ── Live: real Pi and real Ollama (opt-in) ───────────────────────────────────


@pytest.mark.skipif(os.environ.get("AP_LIVE_TESTS") != "1", reason="set AP_LIVE_TESTS=1")
def test_live_pi_qwen_writes_file(tmp_path: Path) -> None:
    registry = load_yaml(ROOT / "policy" / "model_registry.yaml")
    a = PiQwenAdapter.from_registry(
        registry,
        prompt_builder=lambda g: "Create hello.txt in the current directory containing: hi",
        timeout=600,
    )
    g = grant(tmp_path)
    outcome = a.invoke(g, a.prepare(g))
    assert outcome.status == "ok", outcome.detail
    assert (g.write_path / "hello.txt").read_text().strip() == "hi"
