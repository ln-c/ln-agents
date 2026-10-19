"""`python -m agentic_pipeline models {discover,test}`: what is bound, and does it conform.

`discover` is read-only: it never calls a model. `test` makes real model calls through the
model's adapter, outside any run, so it spends the provider's subscription allowance (not
pipeline budget) and applies the dispatcher's own pre-call gates first: the credential
denylist, the model's readiness checks, and the isolation checks its operations need.
"""

from __future__ import annotations

import argparse
import hashlib
import sys
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from agentic_pipeline.conformance import (
    bound_operations,
    conformance_status,
    required_checks,
    run_conformance,
    write_record,
)
from agentic_pipeline.dispatch import DISPATCHER_ENFORCED, BackendAdapter, Identity
from agentic_pipeline.graph import REPO_ROOT, load_yaml
from agentic_pipeline.readiness import Report, Status, run_preflight
from agentic_pipeline.registry import backend_location

PROFILES_FILE = Path("policy") / "model_test_profiles.yaml"


def _default_adapter(registry: Mapping[str, Any], model: str) -> BackendAdapter:
    from agentic_pipeline.run_cli import LazyAdapters  # imports the real adapters lazily

    adapters = LazyAdapters(registry)
    if (why := adapters.unavailable(model)) is not None:
        raise LookupError(why)
    return adapters[model]


@dataclass(slots=True)
class Deps:
    """Everything with side effects, injectable for tests."""

    preflight: Callable[[Path], Report] = run_preflight
    make_adapter: Callable[[Mapping[str, Any], str], BackendAdapter] = _default_adapter
    run_kwargs: dict[str, Any] = field(default_factory=dict)  # new_nonce / clock overrides


def _load(root: Path) -> tuple[Mapping[str, Any], Mapping[str, Any], str]:
    registry = load_yaml(root / "policy" / "model_registry.yaml")
    raw = (root / PROFILES_FILE).read_bytes()
    profiles = load_yaml(root / PROFILES_FILE)
    return registry, profiles, "sha256:" + hashlib.sha256(raw).hexdigest()


def _identity(deps: Deps, registry: Mapping[str, Any], model: str) -> Identity | None:
    try:
        return deps.make_adapter(registry, model).identity()
    except Exception:  # an adapter that cannot be built has no identity to compare
        return None


class CatalogError(Exception):
    """The repository fails its static checks, so nothing about its models can be trusted."""

    def __init__(self, errors: list[str]) -> None:
        super().__init__("; ".join(errors))
        self.errors = errors


def catalog_rows(root: Path, deps: Deps) -> list[dict[str, Any]]:
    """One row per catalog model: what it is, what it is bound to and, when it is bound, whether
    it is ready and has passed conformance. Read-only: it never calls a model.

    `readiness` and `conformance` are None for a model nothing is bound to.
    """
    report = deps.preflight(root)
    if report.static_errors:
        raise CatalogError(list(report.static_errors))
    registry, profiles, profiles_digest = _load(root)
    rows: list[dict[str, Any]] = []
    for name, entry in registry["models"].items():
        bound = bound_operations(registry, name)
        row: dict[str, Any] = {
            "name": name,
            "provider": entry["provider"],
            "model_id": entry["model_id"],
            "family": entry["family"],
            "location": backend_location(registry, name),
            "capabilities": list(entry["capabilities"]),
            "bound_to": [f"{stage}.{op}" for stage, op in bound],
            "readiness": None,
            "conformance": None,
        }
        if bound:
            blockers = report.backend_blockers(name)
            row["readiness"] = "ready" if not blockers else "; ".join(blockers)
            status, detail = conformance_status(
                root, registry, profiles, profiles_digest, name, _identity(deps, registry, name)
            )
            row["conformance"] = {"status": status, "detail": detail}
        rows.append(row)
    return rows


def discover(root: Path, deps: Deps) -> int:
    try:
        rows = catalog_rows(root, deps)
    except CatalogError as exc:
        for err in exc.errors:
            print(f"ERROR: {err}", file=sys.stderr)
        return 1
    for row in rows:
        caps = ", ".join(row["capabilities"]) or "(none claimed)"
        if not row["bound_to"]:
            note = "known, bindable" if row["capabilities"] else "known, unclaimed"
            print(f"{row['name']}  [{row['provider']}:{row['model_id']}, {row['family']}] {note}")
            continue
        print(f"{row['name']}")
        print(f"  model:        {row['provider']}:{row['model_id']}")
        print(f"  family:       {row['family']}")
        print(f"  location:     {row['location']}")
        print(f"  capabilities: {caps}")
        print(f"  bound to:     {', '.join(row['bound_to'])}")
        print(f"  readiness:    {row['readiness']}")
        print(f"  conformance:  {row['conformance']['status']} ({row['conformance']['detail']})")
    return 0


def _needed_isolation(registry: Mapping[str, Any], model: str) -> set[str]:
    """Isolation checks required by the operations this model is bound to."""
    needed: set[str] = set()
    for stage, op_id in bound_operations(registry, model):
        op = next(
            o for o in registry["stage_requirements"][stage]["operations"] if o["id"] == op_id
        )
        needed.update(registry["isolation_profiles"][op["isolation_profile"]])
    return needed


def _refusals(report: Report, registry: Mapping[str, Any], model: str) -> list[str]:
    """Why a conformance call may not be made now (the dispatcher's pre-call gates)."""
    reasons = [
        f"{c.name} {c.status}: {c.detail}"
        for c in report.global_checks
        if c.status is not Status.VERIFIED
    ]
    reasons += report.backend_blockers(model)
    for check in sorted(_needed_isolation(registry, model)):
        found = report.isolation.get(check)
        if found is None:
            reasons.append(f"isolation {check} not reported")
        elif found.status is not Status.VERIFIED:
            reasons.append(f"isolation {check} {found.status}: {found.detail}")
    return list(dict.fromkeys(reasons))


def test(root: Path, model: str, deps: Deps) -> int:
    report = deps.preflight(root)
    for err in report.static_errors:
        print(f"ERROR: {err}", file=sys.stderr)
    if report.static_errors:
        return 1
    registry, profiles, profiles_digest = _load(root)
    if model not in registry["models"]:
        print(f"ERROR: {model!r} is not a model in the catalog", file=sys.stderr)
        return 1
    checks = required_checks(registry, profiles, model)
    if not checks:
        print(f"ERROR: {model} is not bound to any operation; nothing to test", file=sys.stderr)
        return 1
    if reasons := _refusals(report, registry, model):
        print(f"{model}: not testing; the dispatcher would refuse it too:", file=sys.stderr)
        for reason in reasons:
            print(f"  - {reason}", file=sys.stderr)
        return 1
    try:
        adapter = deps.make_adapter(registry, model)
    except Exception as exc:
        print(f"ERROR: no adapter for {model}: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    needed = _needed_isolation(registry, model) - DISPATCHER_ENFORCED
    if gaps := sorted(needed - set(adapter.enforced_isolation)):
        print(f"ERROR: the {model} adapter does not enforce {gaps}", file=sys.stderr)
        return 1
    cost = "local compute" if backend_location(registry, model) == "local" else "its subscription"
    print(f"{model}: running {', '.join(checks)} (live calls use {cost})")
    with tempfile.TemporaryDirectory(prefix="agentic-conformance-") as work:
        record = run_conformance(
            model, adapter, registry, profiles, profiles_digest, Path(work), **deps.run_kwargs
        )
    path, digest = write_record(root, record)
    for check in record["checks"]:
        print(f"  {check['status']:<4}  {check['name']:<12}  {check['detail']}")
    shown = path.relative_to(root).as_posix()
    print(f"{'PASSED' if record['passed'] else 'FAILED'}: {shown} {digest}")
    return 0 if record["passed"] else 1


def add_parser(sub: argparse._SubParsersAction[argparse.ArgumentParser]) -> None:
    models = sub.add_parser("models", help="inspect and conformance-test bound models")
    commands = models.add_subparsers(dest="models_command", required=True)
    for name, text in (
        ("discover", "read-only: the model catalog, bindings, readiness and conformance"),
        ("test", "LIVE: run the conformance probes against one model and record the result"),
    ):
        cmd = commands.add_parser(name, help=text)
        cmd.add_argument("--root", type=Path, default=REPO_ROOT)
        if name == "test":
            cmd.add_argument("--model", required=True, help="a catalog model id")


def models_command(args: argparse.Namespace, deps: Deps | None = None) -> int:
    deps = deps or Deps()
    try:
        if args.models_command == "discover":
            return discover(args.root, deps)
        return test(args.root, args.model, deps)
    except (OSError, KeyError, ValueError) as exc:
        print(f"ERROR: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
