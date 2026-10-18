"""Conformance checks, records, staleness and the `models` commands, with fake adapters only.

No test here calls a real model: the real adapters are never built.
"""
# ruff: noqa: E501

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from agentic_pipeline import conformance, models_cli
from agentic_pipeline.dispatch import AdapterResult, DispatchGrant, Identity, PreparedCall
from agentic_pipeline.graph import load_yaml
from agentic_pipeline.policies import CHECK_NAMES, check_test_profiles
from agentic_pipeline.readiness import Check, Report, Status
from agentic_pipeline.validation import validate_repo

ROOT = Path(__file__).resolve().parents[1]
REGISTRY = load_yaml(ROOT / "policy" / "model_registry.yaml")
PROFILES = load_yaml(ROOT / "policy" / "model_test_profiles.yaml")
PROFILES_DIGEST = (
    "sha256:"
    + hashlib.sha256((ROOT / "policy" / "model_test_profiles.yaml").read_bytes()).hexdigest()
)
NONCE = "ab" * 16
NOW = datetime(2026, 10, 16, 12, 0, 0, tzinfo=UTC)
IDENTITY = Identity("fake-model", "fake 1.0", "fake via test")


class FakeAdapter:
    """Answers the probes the way a compliant model would, unless told to misbehave."""

    enforced_isolation: frozenset[str] = frozenset(
        {"sandbox_enforcement", "session_separation", "protected_paths_read_only"}
        | {"network_disabled", "broker_mediated_inference"}
    )

    def __init__(self, misbehave: str = "", identity: Identity = IDENTITY) -> None:
        self.misbehave = misbehave
        self._identity = identity
        self.calls = 0

    def identity(self) -> Identity:
        return self._identity

    def prepare(self, grant: DispatchGrant) -> PreparedCall:
        i = self._identity
        return PreparedCall(i.model, i.model_version, i.access_method, b"")

    def invoke(self, grant: DispatchGrant, call: PreparedCall) -> AdapterResult:
        self.calls += 1
        if self.misbehave == "raise":
            raise RuntimeError("boom")
        if self.misbehave == "quota":
            return AdapterResult("quota_exhausted", "weekly limit")
        source = next(iter(grant.inputs.values()))
        if grant.output_schema:
            nonce = json.loads(source.read_text())["nonce"]
            reply: Any = {"nonce": "0" * 32 if self.misbehave == "wrong_nonce" else nonce}
            if self.misbehave == "bad_shape":
                reply = {"nonce": nonce, "extra": 1}
            text = "not json" if self.misbehave == "prose" else json.dumps(reply)
            (grant.write_path / f"{grant.operation_id}.json").write_text(text)
        else:
            nonce = source.read_text().strip().splitlines()[-1]
            (grant.write_path / "echo.txt").write_text(
                "0" * 32 if self.misbehave == "wrong_nonce" else nonce
            )
            if self.misbehave == "stray_file":
                (grant.write_path / "extra.txt").write_text("x")
        return AdapterResult("ok")


def run(model: str, adapter: FakeAdapter, tmp_path: Path) -> dict[str, Any]:
    return conformance.run_conformance(
        model, adapter, REGISTRY, PROFILES, PROFILES_DIGEST, tmp_path,
        new_nonce=lambda: NONCE, clock=lambda: NOW,
    )  # fmt: skip


# ── Which checks a model needs ────────────────────────────────────────────


def test_required_checks_follow_the_bound_operations() -> None:
    need = conformance.required_checks
    assert need(REGISTRY, PROFILES, "claude_cli_default") == ["identity", "json_reply"]
    assert need(REGISTRY, PROFILES, "gemini_3_8_flash_high") == ["identity", "json_reply"]
    assert need(REGISTRY, PROFILES, "qwen3_5_9b") == ["identity", "file_output"]
    assert need(REGISTRY, PROFILES, "clef_flash") == []  # bound to nothing


def test_rebinding_changes_the_required_checks() -> None:
    registry = copy.deepcopy(REGISTRY)
    registry["agents"]["build"]["primary"] = "claude_cli_default"
    registry["models"]["claude_cli_default"]["capabilities"].append("code_generation")
    checks = conformance.required_checks(registry, PROFILES, "claude_cli_default")
    assert checks == ["identity", "json_reply", "file_output"]
    assert conformance.required_checks(registry, PROFILES, "qwen3_5_9b") == []


def test_required_checks_for_an_explicit_set_of_capabilities() -> None:
    need = conformance.required_checks
    # a candidate being considered for a role, not yet bound to anything
    assert need(REGISTRY, PROFILES, "gemini_4_argon_high", requires=["design_reasoning"]) == [
        "identity",
        "json_reply",
    ]
    both = need(REGISTRY, PROFILES, "qwen3_5_9b", requires=["design_reasoning", "code_generation"])
    assert both == ["identity", "json_reply", "file_output"]
    assert (
        need(REGISTRY, PROFILES, "qwen3_5_9b", requires=[]) == []
    )  # explicit, so not the bound set
    assert need(REGISTRY, PROFILES, "clef_flash", requires=["classification"]) == [
        "identity",
        "json_reply",
    ]


def test_every_implemented_check_is_known_to_policy_and_the_schema() -> None:
    assert tuple(conformance.CHECKS) == CHECK_NAMES
    schema = json.loads((ROOT / "schemas" / "conformance_result.schema.json").read_text())
    assert schema["properties"]["required_checks"]["items"]["enum"] == list(CHECK_NAMES)


# ── Probes ───────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("model", ["claude_cli_default", "qwen3_5_9b"])
def test_a_compliant_adapter_passes(model: str, tmp_path: Path) -> None:
    record = run(model, FakeAdapter(), tmp_path)
    assert record["passed"] is True
    assert [c["name"] for c in record["checks"]] == record["required_checks"]
    assert record["identity"] == {
        "model": "fake-model",
        "model_version": "fake 1.0",
        "access_method": "fake via test",
    }
    assert record["recorded_at"] == "2026-10-16T12:00:00Z"
    assert record["entry_digest"] == conformance.entry_digest(REGISTRY, model)


@pytest.mark.parametrize(
    ("model", "misbehave", "failing", "detail"),
    [
        ("claude_cli_default", "wrong_nonce", "json_reply", "did not copy the nonce"),
        ("claude_cli_default", "prose", "json_reply", "not a valid conformance_probe"),
        ("claude_cli_default", "bad_shape", "json_reply", "not a valid conformance_probe"),
        ("claude_cli_default", "quota", "json_reply", "quota_exhausted"),
        ("claude_cli_default", "raise", "json_reply", "adapter raised RuntimeError"),
        ("qwen3_5_9b", "wrong_nonce", "file_output", "does not contain the nonce"),
        ("qwen3_5_9b", "stray_file", "file_output", "expected only"),
        ("qwen3_5_9b", "quota", "file_output", "quota_exhausted"),
    ],
)
def test_a_misbehaving_adapter_fails_the_check(
    model: str, misbehave: str, failing: str, detail: str, tmp_path: Path
) -> None:
    record = run(model, FakeAdapter(misbehave), tmp_path)
    assert record["passed"] is False
    by_name = {c["name"]: c for c in record["checks"]}
    assert by_name["identity"]["status"] == "pass"
    assert by_name[failing]["status"] == "fail" and detail in by_name[failing]["detail"]


def test_an_adapter_with_no_identity_is_not_called(tmp_path: Path) -> None:
    adapter = FakeAdapter(identity=Identity("", "v", "a"))
    record = run("claude_cli_default", adapter, tmp_path)
    assert record["passed"] is False and adapter.calls == 0
    assert [c["name"] for c in record["checks"]] == ["identity"]


def test_an_unbound_provider_has_nothing_to_test(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="not bound"):
        run("clef_flash", FakeAdapter(), tmp_path)


def test_each_run_asks_for_a_fresh_nonce(tmp_path: Path) -> None:
    a = conformance.run_conformance(
        "claude_cli_default", FakeAdapter(), REGISTRY, PROFILES, PROFILES_DIGEST, tmp_path / "a"
    )
    b = conformance.run_conformance(
        "claude_cli_default", FakeAdapter(), REGISTRY, PROFILES, PROFILES_DIGEST, tmp_path / "b"
    )
    assert a["passed"] and b["passed"]
    assert (
        len({p.read_text() for d in tmp_path.glob("*/json_reply_input") for p in d.iterdir()}) == 2
    )


# ── Records and staleness ────────────────────────────────────────────────────


def status_of(root: Path, model: str, identity: Identity | None = IDENTITY) -> tuple[str, str]:
    return conformance.conformance_status(
        root, REGISTRY, PROFILES, PROFILES_DIGEST, model, identity
    )


def test_a_written_record_is_current_until_something_it_binds_to_changes(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    record = run("qwen3_5_9b", FakeAdapter(), tmp_path / "work")
    path, digest = conformance.write_record(root, record)
    assert path == root / ".conformance" / "qwen3_5_9b.json"
    assert digest == "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()
    status, detail = status_of(root, "qwen3_5_9b")
    assert status == "ok" and digest in detail


@pytest.mark.parametrize(
    ("identity", "profiles_digest", "reason"),
    [
        (Identity(IDENTITY.model, "fake 2.0", IDENTITY.access_method), PROFILES_DIGEST, "model_version"),
        (Identity("other", IDENTITY.model_version, IDENTITY.access_method), PROFILES_DIGEST, "identity.model "),
        (Identity(IDENTITY.model, IDENTITY.model_version, "else"), PROFILES_DIGEST, "access_method"),
        (IDENTITY, "sha256:" + "1" * 64, "model_test_profiles.yaml changed"),
    ],
)  # fmt: skip
def test_a_record_goes_stale_when_identity_or_profiles_change(
    identity: Identity, profiles_digest: str, reason: str, tmp_path: Path
) -> None:
    record = run("qwen3_5_9b", FakeAdapter(), tmp_path / "work")
    reasons = conformance.stale_reasons(
        record, REGISTRY, PROFILES, profiles_digest, "qwen3_5_9b", identity
    )
    assert any(reason in r for r in reasons), reasons


def test_a_record_goes_stale_when_the_registry_entry_or_bindings_change(tmp_path: Path) -> None:
    record = run("qwen3_5_9b", FakeAdapter(), tmp_path / "work")
    changed = copy.deepcopy(REGISTRY)
    changed["models"]["qwen3_5_9b"]["capabilities"] = ["code_generation"]
    reasons = conformance.stale_reasons(
        record, changed, PROFILES, PROFILES_DIGEST, "qwen3_5_9b", IDENTITY
    )
    assert reasons == ["registry entry for the model or its provider changed"]
    rebound = copy.deepcopy(REGISTRY)
    rebound["agents"]["build"]["primary"] = "claude_cli_default"
    rebound["models"]["claude_cli_default"]["capabilities"].append("code_generation")
    reasons = conformance.stale_reasons(
        record, rebound, PROFILES, PROFILES_DIGEST, "qwen3_5_9b", IDENTITY
    )
    assert any("required checks changed" in r for r in reasons)


def test_status_is_never_ok_without_a_current_passing_record(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    assert status_of(root, "qwen3_5_9b")[0] == "missing"
    failing = run("qwen3_5_9b", FakeAdapter("wrong_nonce"), tmp_path / "work")
    conformance.write_record(root, failing)
    status, detail = status_of(root, "qwen3_5_9b")
    assert status == "failed" and "file_output" in detail
    assert status_of(root, "qwen3_5_9b", Identity("x", "y", "z"))[0] == "stale"
    assert status_of(root, "qwen3_5_9b", None)[0] == "unverifiable"


@pytest.mark.parametrize("damage", ["not json", '{"version": 1}', "[]"])
def test_an_unusable_record_is_unverifiable_not_missing(damage: str, tmp_path: Path) -> None:
    root = tmp_path / "repo"
    path = conformance.record_path(root, "qwen3_5_9b")
    path.parent.mkdir(parents=True)
    path.write_text(damage)
    status, _ = status_of(root, "qwen3_5_9b")
    assert status == "unverifiable"


def test_a_record_for_another_provider_is_refused(tmp_path: Path) -> None:
    root = tmp_path / "repo"
    record = run("claude_cli_default", FakeAdapter(), tmp_path / "work")
    shutil.copy(
        conformance.write_record(root, record)[0], conformance.record_path(root, "qwen3_5_9b")
    )
    assert status_of(root, "qwen3_5_9b")[0] == "unverifiable"


def test_writing_an_invalid_record_is_refused(tmp_path: Path) -> None:
    record = run("qwen3_5_9b", FakeAdapter(), tmp_path / "work")
    record["required_checks"] = []
    with pytest.raises(Exception, match="required_checks"):
        conformance.write_record(tmp_path / "repo", record)


# ── Profiles policy ──────────────────────────────────────────────────────────

CAPS = ["classification", "code_generation", "coordination", "decision", "design_reasoning"]


def test_the_repo_profiles_and_schemas_validate() -> None:
    assert validate_repo(ROOT) == []
    assert check_test_profiles(PROFILES, CAPS) == []


@pytest.mark.parametrize(
    ("profiles", "expected"),
    [
        ({"design_reasoning": ["identity"], "code_generation": ["identity"]}, "no profile for"),
        ({**PROFILES["profiles"], "telepathy": ["identity"]}, "not a registry capability"),
        ({**PROFILES["profiles"], "code_generation": ["json_reply"]}, "start with the identity"),
        ({**PROFILES["profiles"], "code_generation": ["identity", "vibes"]}, "unknown check"),
        ({**PROFILES["profiles"], "code_generation": ["identity", "identity"]}, "repeats"),
        ({**PROFILES["profiles"], "code_generation": []}, "non-empty list"),
    ],
)
def test_bad_profiles_are_rejected(profiles: dict[str, Any], expected: str) -> None:
    errors = check_test_profiles({"profiles": profiles}, CAPS)
    assert any(expected in e for e in errors), errors


# ── The models commands ──────────────────────────────────────────────────────


def report(drop: str | None = None, **changes: Status) -> Report:
    ok = Check("x", Status.VERIFIED, "ok")
    isolation = {
        n: Check(n, changes.get(n, Status.VERIFIED), "d")
        for n in REGISTRY["isolation_profiles"]["implementation_sandbox"]
        + REGISTRY["isolation_profiles"]["independent_cross_critique"]
        + REGISTRY["isolation_profiles"]["sealed_inputs_only"]
    }
    return Report(
        static_errors=[],
        global_checks=[Check("credential_env_absent", changes.get("env", Status.VERIFIED), "d")],
        providers={n: [ok] for n in ("claude_pro", "google_ai_pro", "llamacpp_local")},
        models={"qwen3_5_9b": [Check("model_inventory", Status.VERIFIED, "")]},
        isolation={k: v for k, v in isolation.items() if k != drop},
        registry=REGISTRY,
    )


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    shutil.copytree(ROOT / "policy", root / "policy")
    return root


def deps(adapter: FakeAdapter | None, rep: Report | None = None) -> models_cli.Deps:
    def make(_: Any, model: str) -> FakeAdapter:
        if adapter is None:
            raise LookupError("no adapter here")
        return adapter

    return models_cli.Deps(
        preflight=lambda _root: rep or report(),
        make_adapter=make,
        run_kwargs={"new_nonce": lambda: NONCE, "clock": lambda: NOW},
    )


def args(command: str, **kw: Any) -> argparse.Namespace:
    return argparse.Namespace(models_command=command, **kw)


def test_models_test_records_a_passing_result(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = models_cli.models_command(
        args("test", root=repo, model="qwen3_5_9b"), deps(FakeAdapter())
    )
    out = capsys.readouterr().out
    assert code == 0 and "PASSED" in out and ".conformance/qwen3_5_9b.json" in out
    record, _ = conformance.load_record(repo, "qwen3_5_9b")
    assert record is not None and record["passed"] is True


def test_models_test_records_and_reports_a_failure(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = models_cli.models_command(
        args("test", root=repo, model="qwen3_5_9b"), deps(FakeAdapter("wrong_nonce"))
    )
    assert code == 1 and "FAILED" in capsys.readouterr().out
    record, _ = conformance.load_record(repo, "qwen3_5_9b")
    assert record is not None and record["passed"] is False


@pytest.mark.parametrize(
    ("model", "rep", "adapter", "why"),
    [
        ("qwen3_5_9b", report(env=Status.FAILED), FakeAdapter(), "credential_env_absent"),
        ("qwen3_5_9b", report(network_disabled=Status.UNVERIFIED), FakeAdapter(), "network_disabled"),
        ("claude_opus_5_5_high_cli", report(session_separation=Status.FAILED), FakeAdapter(), "session_separation"),
        ("qwen3_5_9b", report(drop="network_disabled"), FakeAdapter(), "network_disabled not reported"),
        ("qwen3_5_9b", None, None, "no adapter for qwen3_5_9b"),
        ("clef_flash", None, FakeAdapter(), "not bound"),
        ("gpt_paid", None, FakeAdapter(), "not a model in the catalog"),
    ],
)  # fmt: skip
def test_models_test_refuses_where_the_dispatcher_would(
    model: str,
    rep: Report | None,
    adapter: FakeAdapter | None,
    why: str,
    repo: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    code = models_cli.models_command(args("test", root=repo, model=model), deps(adapter, rep))
    assert code == 1 and why in capsys.readouterr().err
    assert adapter is None or adapter.calls == 0
    assert conformance.load_record(repo, model)[0] is None


def test_models_test_refuses_a_provider_with_a_failing_readiness_check(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    rep = report()
    rep.providers["llamacpp_local"] = [Check("daemon", Status.FAILED, "down")]
    adapter = FakeAdapter()
    code = models_cli.models_command(
        args("test", root=repo, model="qwen3_5_9b"), deps(adapter, rep)
    )
    assert code == 1 and "qwen3_5_9b.daemon FAILED" in capsys.readouterr().err
    assert adapter.calls == 0


def test_models_discover_reports_each_provider_and_never_calls_a_model(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    adapter = FakeAdapter()
    conformance.write_record(repo, run("qwen3_5_9b", adapter, repo / "w"))
    adapter.calls = 0
    code = models_cli.models_command(args("discover", root=repo), deps(adapter))
    out = capsys.readouterr().out
    assert code == 0 and adapter.calls == 0
    assert "claude_cli_default" in out and "conformance:  missing" in out
    # the fake's identity is the one recorded, so qwen3_5_9b is current
    assert "conformance:  ok" in out
    assert "clef_flash  [clef_flash_free:clef-flash, clef] known, bindable" in out
    assert "gemini_4_argon_high" in out and "known, unclaimed" in out


def test_models_discover_cannot_vouch_for_a_provider_whose_adapter_is_unavailable(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    conformance.write_record(repo, run("qwen3_5_9b", FakeAdapter(), repo / "w"))
    models_cli.models_command(args("discover", root=repo), deps(None))
    assert "conformance:  unverifiable" in capsys.readouterr().out


def test_static_errors_stop_both_commands(repo: Path, capsys: pytest.CaptureFixture[str]) -> None:
    broken = lambda _root: Report(static_errors=["registry R1: broken"])  # noqa: E731
    d = models_cli.Deps(preflight=broken)
    assert models_cli.models_command(args("discover", root=repo), d) == 1
    assert models_cli.models_command(args("test", root=repo, model="qwen3_5_9b"), d) == 1
    assert capsys.readouterr().err.count("registry R1: broken") == 2


def test_models_test_refuses_an_adapter_that_does_not_enforce_the_isolation_it_needs(
    repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    adapter = FakeAdapter()
    adapter.enforced_isolation = frozenset({"sandbox_enforcement"})
    code = models_cli.models_command(args("test", root=repo, model="qwen3_5_9b"), deps(adapter))
    assert code == 1 and "does not enforce" in capsys.readouterr().err
    assert adapter.calls == 0
