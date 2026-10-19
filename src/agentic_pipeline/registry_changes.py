"""Changes to the model registry, drafted by code and applied only when a human confirms.

`/add_model`, `/remove_model` and `/agents <slot> <model>` in ln-agents each draft one change to
`policy/model_registry.yaml`. A draft is made by code from the request's own fields (a model
never writes the file, and nothing here parses model text), checked before it is stored:

* the new text is parsed and must hold exactly the registry the change means to produce;
* the whole repository is validated with that registry (R14 independence, R18 capabilities,
  R17 families, R20 specs, the data classes, ...), so a change that would break a rule is
  refused with the rule's own message and nothing is stored;
* the draft keeps the digest of the file it was made from, the new text and a diff written by
  code, under `<runs>/.changes/<id>.json`. Nothing in `policy/` changes yet.

`apply` is what the human's `/confirm <id>` calls. It writes the file only if the registry is
still the one the draft was made from, and validates the new text again. It cannot write
anything but a stored draft, so the caller supplies an id and never any text. A human then
commits and pushes the edit signed, as for any change to policy: until then it is only a change
to the working tree, and a run in progress keeps the policy it froze at its start.
"""

from __future__ import annotations

import copy
import difflib
import hashlib
import json
import re
import secrets
import shutil
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agentic_pipeline import registry_edit
from agentic_pipeline.graph import loads_yaml
from agentic_pipeline.registry import (
    BACKEND_KEYS,
    _backend_refs,
    agent_spec_path,
    backend_family,
    backend_location,
    operations,
    pattern_family,
)
from agentic_pipeline.validation import _capabilities, validate_repo

CHANGES_DIR = ".changes"
REGISTRY = Path("policy") / "model_registry.yaml"
ID_RE = re.compile(r"^[0-9a-f]{8}$")
MAX_MODEL_ID = 200
MAX_LIST = 8


class ChangeError(Exception):
    """A change that cannot be drafted or applied; `errors` say why, one per line."""

    def __init__(self, *errors: str) -> None:
        super().__init__("; ".join(errors))
        self.errors = list(errors)


@dataclass(frozen=True, slots=True)
class Draft:
    change_id: str
    kind: str
    summary: str
    notes: list[str]
    diff: str


@dataclass(frozen=True, slots=True)
class Applied:
    change_id: str
    summary: str
    path: str


def _registry_bytes(root: Path) -> bytes:
    try:
        return (root / REGISTRY).read_bytes()
    except OSError as exc:
        raise ChangeError(f"cannot read {REGISTRY}: {exc}") from exc


def _digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _load(data: bytes) -> dict[str, Any]:
    registry = loads_yaml(data)
    if not isinstance(registry, dict):
        raise ChangeError(f"{REGISTRY} is not a YAML mapping")
    return registry


def _validate(root: Path, text: str) -> list[str]:
    """Every static violation the repository would have with `text` as its registry."""
    with tempfile.TemporaryDirectory(prefix="ln-registry-change-") as tmp:
        stage = Path(tmp)
        for name in ("pipeline", "policy"):
            shutil.copytree(root / name, stage / name)
        (stage / REGISTRY).write_text(text, encoding="utf-8", newline="")
        return validate_repo(root, stage)


# ── The slots a model can be bound to ────────────────────────────────────────


def slots(registry: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Every backend slot under `agents`: where it is, what it holds, and what runs on it."""
    models = registry["models"]
    found: list[dict[str, Any]] = []

    def walk(node: Mapping[str, Any], path: list[str]) -> None:
        for key, value in node.items():
            if isinstance(value, Mapping):
                walk(value, [*path, key])
            elif key in BACKEND_KEYS and isinstance(value, str | list):
                refs = [value] if isinstance(value, str) else list(value)
                agent = ".".join(path)
                found.append(
                    {
                        "slot": ".".join([*path, key]),
                        "agent": agent,
                        "spec": agent_spec_path(registry, path[0]),
                        "value": value,
                        "models": [
                            {
                                "name": ref,
                                "family": backend_family(registry, ref),
                                "location": backend_location(registry, ref),
                            }
                            for ref in refs
                            if ref in models
                        ],
                        # Only a slot that holds nothing but catalog models is a model slot: a
                        # `deterministic_rules` primary is the design, not a choice of model.
                        "rebindable": all(ref in models for ref in refs),
                        "operations": [
                            f"{stage}.{op['id']}"
                            for stage, op in operations(registry["stage_requirements"])
                            if op["agent"] == agent
                        ],
                    }
                )

    walk(registry["agents"], [])
    return found


def mapping(root: Path) -> list[dict[str, Any]]:
    return slots(_load(_registry_bytes(root)))


# ── Drafting ─────────────────────────────────────────────────────────────────


def _id() -> str:
    return secrets.token_hex(4)


def _store(runs_dir: Path, record: Mapping[str, Any]) -> None:
    folder = runs_dir / CHANGES_DIR
    folder.mkdir(parents=True, exist_ok=True)
    (folder / f"{record['id']}.json").write_text(
        json.dumps(record, indent=2, sort_keys=True), encoding="utf-8"
    )


def _finish(
    root: Path,
    runs_dir: Path,
    *,
    kind: str,
    summary: str,
    notes: list[str],
    old: bytes,
    new_text: str,
    expected: Mapping[str, Any],
) -> Draft:
    if loads_yaml(new_text.encode("utf-8")) != expected:
        raise ChangeError("the text edit did not produce the registry it meant to; nothing saved")
    if errors := _validate(root, new_text):
        raise ChangeError(*errors)
    diff = "".join(
        difflib.unified_diff(
            old.decode("utf-8").splitlines(keepends=True),
            new_text.splitlines(keepends=True),
            fromfile=f"a/{REGISTRY.as_posix()}",
            tofile=f"b/{REGISTRY.as_posix()}",
            n=2,
        )
    )
    change_id = _id()
    _store(
        runs_dir,
        {
            "id": change_id,
            "kind": kind,
            "summary": summary,
            "notes": notes,
            "diff": diff,
            "base": _digest(old),
            "new_text": new_text,
            "created_at": datetime.now(UTC).isoformat(),
        },
    )
    return Draft(change_id, kind, summary, notes, diff)


def _model_name(model_id: str, taken: Mapping[str, Any]) -> str:
    name = re.sub(r"[^a-z0-9]+", "_", model_id.lower()).strip("_") or "model"
    if not re.match(r"[a-z_]", name):
        name = f"m_{name}"
    if name in taken:
        raise ChangeError(f"a model named {name!r} is already in the catalog")
    return name


def draft_add_model(
    root: Path, runs_dir: Path, provider: str, model_id: str, capabilities: Sequence[str]
) -> Draft:
    """Add a model of `provider` to the catalog; its family is the provider's pattern's.

    A model the catalog already has (same provider and id) is not added twice: the capabilities
    named are claimed for it, which is how a known but unclaimed model becomes bindable.
    """
    old = _registry_bytes(root)
    registry = _load(old)
    spec = registry["providers"].get(provider)
    if spec is None or "family_patterns" not in spec:
        models = sorted(p for p, s in registry["providers"].items() if "family_patterns" in s)
        raise ChangeError(f"{provider!r} is not a model provider; the model providers are {models}")
    if not model_id or len(model_id) > MAX_MODEL_ID or not model_id.isprintable():
        raise ChangeError("the model id must be a short printable string")
    vocabulary = _capabilities(root)
    capabilities = list(dict.fromkeys(capabilities))
    if unknown := [c for c in capabilities if c not in vocabulary]:
        raise ChangeError(f"unknown capabilities {unknown}; the vocabulary is {vocabulary}")
    for name, known in registry["models"].items():
        if known["provider"] == provider and known["model_id"] == model_id:
            return _draft_claim(root, runs_dir, old, registry, name, capabilities)
    if "launch" in spec:
        raise ChangeError(
            f"{provider} starts one fixed server (its `launch` names the model it serves); "
            "adding a model to it is an edit to its launch, so make it by hand"
        )
    family = pattern_family(spec, model_id)
    if family is None:
        raise ChangeError(f"no family pattern of {provider} matches {model_id!r}")
    name = _model_name(model_id, registry["models"])
    entry = {
        "provider": provider,
        "model_id": model_id,
        "alias": model_id,
        "family": family,
        "capabilities": capabilities,
    }
    expected = copy.deepcopy(registry)
    expected["models"][name] = entry
    try:
        new_text = registry_edit.add_model(old.decode("utf-8"), name, entry)
    except registry_edit.EditError as exc:
        raise ChangeError(str(exc)) from exc
    notes = [
        f"{name} is a {family} model served through {provider}"
        f" ({'its traffic leaves this host' if spec.get('location') != 'local' else 'local'})."
    ]
    notes += _claim_notes(name, capabilities)
    return _finish(
        root,
        runs_dir,
        kind="add_model",
        summary=f"Add model {name} ({provider}:{model_id})",
        notes=notes,
        old=old,
        new_text=new_text,
        expected=expected,
    )


def _claim_notes(name: str, capabilities: Sequence[str]) -> list[str]:
    if not capabilities:
        return [
            "It claims no capability, so it is known but cannot be bound to anything until a "
            "human claims some."
        ]
    return [
        f"You claim these capabilities for it: {', '.join(capabilities)}. A capability is "
        f"declared, not measured; `python -m agentic_pipeline models test --model {name}` "
        "checks it once the model is bound."
    ]


def _draft_claim(
    root: Path,
    runs_dir: Path,
    old: bytes,
    registry: Mapping[str, Any],
    name: str,
    capabilities: Sequence[str],
) -> Draft:
    have = list(registry["models"][name]["capabilities"])
    new = [*have, *(c for c in capabilities if c not in have)]
    if new == have:
        said = f"already claims {', '.join(have)}" if have else "claims nothing"
        raise ChangeError(
            f"{name} is already in the catalog and {said}; name capabilities to claim"
            if not capabilities
            else f"{name} already claims {', '.join(capabilities)}"
        )
    expected = copy.deepcopy(dict(registry))
    expected["models"][name]["capabilities"] = new
    try:
        new_text = registry_edit.set_value(
            old.decode("utf-8"), ["models", name, "capabilities"], new
        )
    except registry_edit.EditError as exc:
        raise ChangeError(str(exc)) from exc
    return _finish(
        root,
        runs_dir,
        kind="claim_capabilities",
        summary=f"Claim {', '.join(new[len(have) :])} for {name}",
        notes=[f"{name} is already in the catalog."] + _claim_notes(name, new[len(have) :]),
        old=old,
        new_text=new_text,
        expected=expected,
    )


def _bound_to(registry: Mapping[str, Any], model: str) -> list[str]:
    return [
        where.removeprefix("agents.")
        for where, ref in _backend_refs(registry["agents"], "agents")
        if ref == model
    ]


def draft_remove_model(root: Path, runs_dir: Path, model: str) -> Draft:
    old = _registry_bytes(root)
    registry = _load(old)
    if model not in registry["models"]:
        raise ChangeError(f"{model!r} is not a model in the catalog")
    if bound := _bound_to(registry, model):
        raise ChangeError(
            f"{model} is bound to {', '.join(bound)}; bind those slots to another model with "
            "/agents first"
        )
    if model in _coordinators(root):
        raise ChangeError(f"{model} is the coordinator named in LN-AGENTS.md; choose another first")
    expected = copy.deepcopy(registry)
    del expected["models"][model]
    try:
        new_text = registry_edit.remove_model(old.decode("utf-8"), model)
    except registry_edit.EditError as exc:
        raise ChangeError(str(exc)) from exc
    return _finish(
        root,
        runs_dir,
        kind="remove_model",
        summary=f"Remove model {model}",
        notes=[
            "Nothing is bound to it. Conformance records under .conformance/ are left in place."
        ],
        old=old,
        new_text=new_text,
        expected=expected,
    )


def _coordinators(root: Path) -> list[str]:
    from agentic_pipeline.coordinator import SetupError, load_setup

    try:
        setup = load_setup(root)
    except SetupError:
        return []
    return [m for m in (setup.coordinator, setup.fallback) if m] if setup else []


def draft_bind(root: Path, runs_dir: Path, slot: str, models: Sequence[str]) -> Draft:
    """Bind `slot` (e.g. `design.proposal_b.primary`) to `models`, a list for a list slot."""
    old = _registry_bytes(root)
    registry = _load(old)
    by_slot = {s["slot"]: s for s in slots(registry)}
    row = by_slot.get(slot)
    if row is None:
        raise ChangeError(f"no slot {slot!r}; the slots are {sorted(by_slot)}")
    if not row["rebindable"]:
        raise ChangeError(
            f"{slot} holds {row['value']!r}, which is not a model; it is not rebound here"
        )
    models = list(models)
    if not models or len(models) > MAX_LIST or len(set(models)) != len(models):
        raise ChangeError("name one to eight distinct models")
    if unknown := [m for m in models if m not in registry["models"]]:
        raise ChangeError(f"not in the catalog: {unknown}")
    is_list = isinstance(row["value"], list)
    if not is_list and len(models) != 1:
        raise ChangeError(f"{slot} takes one model, not {len(models)}")
    value: str | list[str] = models if is_list else models[0]
    if value == row["value"]:
        raise ChangeError(f"{slot} is already {value}")
    path = slot.split(".")
    expected = copy.deepcopy(registry)
    node = expected["agents"]
    for key in path[:-1]:
        node = node[key]
    node[path[-1]] = value
    try:
        new_text = registry_edit.set_slot(old.decode("utf-8"), path, value)
    except registry_edit.EditError as exc:
        raise ChangeError(str(exc)) from exc
    notes = []
    for model in models:
        if backend_location(registry, model) != "local":
            provider = registry["models"][model]["provider"]
            notes.append(
                f"{model} runs through {provider}: what {row['agent']} is sent leaves this host "
                "(policy/data_classes.yaml still decides what may go)."
            )
    if row["operations"]:
        notes.append(f"It serves {', '.join(row['operations'])}.")
    return _finish(
        root,
        runs_dir,
        kind="bind",
        summary=f"Bind {slot}: {row['value']} -> {value}",
        notes=notes,
        old=old,
        new_text=new_text,
        expected=expected,
    )


# ── Confirming ───────────────────────────────────────────────────────────────


def _record(runs_dir: Path, change_id: str) -> tuple[Path, dict[str, Any]]:
    if not ID_RE.fullmatch(change_id):
        raise ChangeError(f"{change_id!r} is not a change id (8 hex digits)")
    path = runs_dir / CHANGES_DIR / f"{change_id}.json"
    try:
        return path, json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ChangeError(f"no pending change {change_id}") from exc
    except (OSError, ValueError) as exc:
        raise ChangeError(f"change {change_id} is unreadable: {exc}") from exc


def apply(root: Path, runs_dir: Path, change_id: str) -> Applied:
    """Write a stored draft to `policy/model_registry.yaml` if nothing has changed under it."""
    from agentic_pipeline.fsutil import atomic_write_text

    path, record = _record(runs_dir, change_id)
    if _digest(_registry_bytes(root)) != record["base"]:
        raise ChangeError(
            f"{REGISTRY} changed after change {change_id} was drafted; discard it and draft "
            "it again"
        )
    if errors := _validate(root, record["new_text"]):
        raise ChangeError(*errors)
    atomic_write_text(root / REGISTRY, record["new_text"])
    path.unlink(missing_ok=True)
    return Applied(change_id, record["summary"], REGISTRY.as_posix())


def discard(runs_dir: Path, change_id: str) -> str:
    path, record = _record(runs_dir, change_id)
    path.unlink(missing_ok=True)
    return str(record["summary"])


def pending(runs_dir: Path) -> list[dict[str, str]]:
    """The drafts waiting for a human, oldest first."""
    found = []
    for path in sorted((runs_dir / CHANGES_DIR).glob("*.json")):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
            found.append(
                {
                    "id": record["id"],
                    "summary": record["summary"],
                    "created_at": record["created_at"],
                }
            )
        except (OSError, ValueError, KeyError):
            continue
    return sorted(found, key=lambda r: r["created_at"])
