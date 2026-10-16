"""Conformance results: evidence that a bound model follows the pipeline's output contract.

`models test` runs a small probe per required check through the provider's real adapter and
records the outcome in `.conformance/<provider>.json`. A probe is a smoke test: it shows the
model can read a sealed input and answer in the shape an operation needs, not that it is good
at the role, and it never replaces the project's acceptance tests.

Which checks a provider needs is derived from the operations it can be bound to: the union,
over their `requires`, of the checks listed in policy/model_test_profiles.yaml. A record is
current only while the provider's identity (model, version, access path), its registry entry,
the profiles file and that derived list all still match what was recorded; any change makes it
stale, and a stale or missing record is never read as a pass.

Limits: records are unsigned, so they are tamper-evident only through their digest, and the
repository root is not writable from a sandboxed model. A provider that cannot report which
model answered (agy) is identified by the model the registry requested.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import uuid
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agentic_pipeline.dispatch import BackendAdapter, DispatchGrant, Identity
from agentic_pipeline.fsutil import atomic_write_json
from agentic_pipeline.policies import CHECK_NAMES
from agentic_pipeline.registry import op_backends, operations
from agentic_pipeline.schemas import ValidationError, validator

RECORD_DIR = ".conformance"
PROBE_SCHEMA = "conformance_probe"
STAGE = "CONFORMANCE"
EXPECTED_FILE = "echo.txt"
IDENTITY_FIELDS = ("model", "model_version", "access_method")
type Probe = Callable[["ProbeEnv"], tuple[bool, str]]


@dataclass(frozen=True, slots=True)
class ProbeEnv:
    provider: str
    adapter: BackendAdapter
    work: Path  # scratch directory for this run, owned by the caller
    nonce: str


def digest_of(document: Any) -> str:
    """sha256 of the canonical JSON of a document."""
    raw = json.dumps(document, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def provider_digest(registry: Mapping[str, Any], provider: str) -> str:
    return digest_of(registry["providers"][provider])


def bound_operations(registry: Mapping[str, Any], provider: str) -> list[tuple[str, str]]:
    """(stage, operation id) for every operation this provider may run."""
    return [
        (stage, op["id"])
        for stage, op in operations(registry["stage_requirements"])
        if provider in op_backends(registry, stage, op)
    ]


def required_checks(
    registry: Mapping[str, Any], profiles: Mapping[str, Any], provider: str
) -> list[str]:
    """Checks the provider must pass, from what its bound operations require of it."""
    wanted: set[str] = set()
    for stage, op in operations(registry["stage_requirements"]):
        if provider in op_backends(registry, stage, op):
            for capability in op["requires"]:
                wanted.update(profiles["profiles"][capability])
    return [name for name in CHECK_NAMES if name in wanted]


# ── Probes ───────────────────────────────────────────────────────────────────


def _grant(env: ProbeEnv, operation: str, inputs: Mapping[str, Path], schema: str | None) -> Any:
    attempt = f"{STAGE}-{uuid.uuid4().hex[:12]}"
    write_path = env.work / attempt / operation
    write_path.mkdir(parents=True, exist_ok=False)  # the dispatcher's convention
    return DispatchGrant(
        run_id="conformance",
        stage=STAGE,
        operation_id=operation,
        backend=env.provider,
        invocation_id=uuid.uuid4().hex,
        role="conformance_probe",
        allowed_reads=tuple(sorted(inputs)),
        inputs=dict(inputs),
        data_class="public",  # synthetic input only
        write_root="conformance",
        attempt=attempt,
        write_path=write_path,
        output_schema=schema,
    )


def _call(env: ProbeEnv, grant: DispatchGrant) -> str | None:
    """Run the adapter on a grant; the reason it did not answer, or None."""
    try:
        result = env.adapter.invoke(grant, env.adapter.prepare(grant))
    except Exception as exc:  # a probe reports any adapter failure instead of raising
        return f"adapter raised {type(exc).__name__}: {exc}"
    return None if result.status == "ok" else f"{result.status}: {result.detail}".rstrip()


def check_identity(env: ProbeEnv) -> tuple[bool, str]:
    try:
        identity = env.adapter.identity()
    except Exception as exc:
        return False, f"identity() raised {type(exc).__name__}: {exc}"
    empty = [k for k, v in asdict(identity).items() if not isinstance(v, str) or not v.strip()]
    if empty:
        return False, f"identity has empty fields: {empty}"
    return True, f"{identity.model} / {identity.model_version}"


def check_json_reply(env: ProbeEnv) -> tuple[bool, str]:
    source = env.work / "json_reply_input" / "probe_input.json"
    source.parent.mkdir(parents=True)
    source.write_text(json.dumps({"nonce": env.nonce}), encoding="utf-8")
    grant = _grant(env, "json_reply", {"probe_input": source}, PROBE_SCHEMA)
    if why := _call(env, grant):
        return False, why
    reply = grant.write_path / "json_reply.json"
    try:
        document = json.loads(reply.read_text(encoding="utf-8"))
        validator(PROBE_SCHEMA)(document)
    except (OSError, ValueError, ValidationError) as exc:
        return False, f"reply is not a valid {PROBE_SCHEMA} object: {type(exc).__name__}: {exc}"
    if document["nonce"] != env.nonce:
        return False, "reply did not copy the nonce from the sealed input"
    return True, "reply validated and copied the nonce"


def check_file_output(env: ProbeEnv) -> tuple[bool, str]:
    # The Pi prompt carries no task line, so the instruction travels in the input file.
    source = env.work / "file_output_input" / "INSTRUCTIONS.md"
    source.parent.mkdir(parents=True)
    source.write_text(
        f"Write a file named {EXPECTED_FILE} in the current directory. Its entire content must "
        f"be exactly this token and nothing else:\n{env.nonce}\n",
        encoding="utf-8",
    )
    grant = _grant(env, "file_output", {"probe_input": source}, None)
    if why := _call(env, grant):
        return False, why
    files = sorted(p.relative_to(grant.write_path).as_posix() for p in grant.write_path.rglob("*"))
    if files != [EXPECTED_FILE]:
        return False, f"write root holds {files}, expected only [{EXPECTED_FILE!r}]"
    if (grant.write_path / EXPECTED_FILE).read_text(encoding="utf-8").strip() != env.nonce:
        return False, f"{EXPECTED_FILE} does not contain the nonce"
    return True, f"wrote only {EXPECTED_FILE} with the nonce"


CHECKS: Mapping[str, Probe] = {
    "identity": check_identity,
    "json_reply": check_json_reply,
    "file_output": check_file_output,
}
assert tuple(CHECKS) == CHECK_NAMES


def run_conformance(
    provider: str,
    adapter: BackendAdapter,
    registry: Mapping[str, Any],
    profiles: Mapping[str, Any],
    profiles_digest: str,
    work: Path,
    *,
    new_nonce: Callable[[], str] = lambda: secrets.token_hex(16),
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> dict[str, Any]:
    """Run every required check and return the (schema-valid) record; never raises on a miss."""
    required = required_checks(registry, profiles, provider)
    if not required:
        raise ValueError(f"{provider} is not bound to any operation; there is nothing to test")
    env = ProbeEnv(provider, adapter, work, new_nonce())
    results: list[dict[str, str]] = []
    for name in required:
        ok, detail = CHECKS[name](env)
        results.append({"name": name, "status": "pass" if ok else "fail", "detail": detail})
        if name == "identity" and not ok:
            break  # no point calling a model whose identity cannot be pinned
    try:
        identity = asdict(adapter.identity())
    except Exception:  # the identity check already recorded why
        identity = {}
    # A record needs a value in every field, so a missing one reads "(none)"; it fails anyway.
    identity = {k: str(identity.get(k) or "").strip() or "(none)" for k in IDENTITY_FIELDS}
    record: dict[str, Any] = {
        "version": 1,
        "provider": provider,
        "identity": identity,
        "provider_digest": provider_digest(registry, provider),
        "profiles_digest": profiles_digest,
        "required_checks": required,
        "checks": results,
        "passed": len(results) == len(required) and all(r["status"] == "pass" for r in results),
        "recorded_at": clock().strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    validator("conformance_result")(record)
    return record


# ── Records ──────────────────────────────────────────────────────────────────


def record_path(root: Path, provider: str) -> Path:
    return root / RECORD_DIR / f"{provider}.json"


def write_record(root: Path, record: Mapping[str, Any]) -> tuple[Path, str]:
    """Validate and atomically write a record; returns (path, sha256 of its bytes)."""
    validator("conformance_result")(record)
    path = record_path(root, record["provider"])
    atomic_write_json(path, record)
    return path, "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def load_record(root: Path, provider: str) -> tuple[dict[str, Any] | None, str]:
    """(record, digest of its bytes), or (None, why) when it is absent or unusable."""
    path = record_path(root, provider)
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return None, "no record"
    except OSError as exc:
        return None, f"unreadable: {exc}"
    try:
        record = json.loads(raw)
        validator("conformance_result")(record)
    except (ValueError, ValidationError) as exc:
        return None, f"invalid record: {exc}"
    if record["provider"] != provider:
        return None, f"record is for {record['provider']!r}"
    return record, "sha256:" + hashlib.sha256(raw).hexdigest()


def stale_reasons(
    record: Mapping[str, Any],
    registry: Mapping[str, Any],
    profiles: Mapping[str, Any],
    profiles_digest: str,
    provider: str,
    identity: Identity,
) -> list[str]:
    """Why a record no longer describes the provider as it is now (empty means current)."""
    reasons: list[str] = []
    for key, now in asdict(identity).items():
        if record["identity"][key] != now:
            reasons.append(f"identity.{key} changed")
    if record["provider_digest"] != provider_digest(registry, provider):
        reasons.append("registry entry for the provider changed")
    if record["profiles_digest"] != profiles_digest:
        reasons.append("policy/model_test_profiles.yaml changed")
    if record["required_checks"] != required_checks(registry, profiles, provider):
        reasons.append("required checks changed (the bound operations changed)")
    return reasons


def conformance_status(
    root: Path,
    registry: Mapping[str, Any],
    profiles: Mapping[str, Any],
    profiles_digest: str,
    provider: str,
    identity: Identity | None,
) -> tuple[str, str]:
    """(status, detail): ok | failed | stale | missing | unverifiable. Only `ok` is a pass."""
    record, digest_or_why = load_record(root, provider)
    if record is None:
        return ("missing" if digest_or_why == "no record" else "unverifiable"), digest_or_why
    if identity is None:
        return "unverifiable", "the provider's current identity could not be resolved"
    if reasons := stale_reasons(record, registry, profiles, profiles_digest, provider, identity):
        return "stale", "; ".join(reasons)
    if not record["passed"]:
        failed = [c["name"] for c in record["checks"] if c["status"] == "fail"]
        return "failed", f"failed checks: {failed or ['(incomplete)']}"
    return "ok", f"recorded {record['recorded_at']}, {digest_or_why}"
