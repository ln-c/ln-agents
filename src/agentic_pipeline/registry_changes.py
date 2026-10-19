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
    Gap,
    _backend_refs,
    agent_spec_path,
    backend_family,
    backend_location,
    operations,
    pattern_family,
    spec_gaps,
)
from agentic_pipeline.validation import _capabilities, validate_repo

CHANGES_DIR = ".changes"
REGISTRY = Path("policy") / "model_registry.yaml"
ID_RE = re.compile(r"^[0-9a-f]{8}$")
MAX_MODEL_ID = 200
MAX_LIST = 8


NO_MODEL_HINT = (
    "Some agents have no model, and no change can be made until each has one: "
    "/start proposes a model for them."
)


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


def _refused(errors: Sequence[str]) -> ChangeError:
    """The refusal for `errors`; it says what to do when the cause is an agent with no model."""
    gap = any(
        e.startswith("registry R20") and ("names no catalog model" in e or "is not the spec" in e)
        for e in errors
    )
    return ChangeError(*errors, *([NO_MODEL_HINT] if gap else []))


def _same_pending(runs_dir: Path, kind: str, old: bytes, new_text: str) -> Draft | None:
    for path in sorted((runs_dir / CHANGES_DIR).glob("*.json")):
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
            if (record["kind"], record["base"], record["new_text"]) == (
                kind,
                _digest(old),
                new_text,
            ):
                return Draft(
                    record["id"], record["kind"], record["summary"], record["notes"], record["diff"]
                )
        except (OSError, ValueError, KeyError):
            continue
    return None


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
    reuse: bool = False,
) -> Draft:
    """Check the edited text and store it as a draft. With `reuse`, a draft already waiting
    for exactly this change is returned instead of a second one."""
    if loads_yaml(new_text.encode("utf-8")) != expected:
        raise ChangeError("the text edit did not produce the registry it meant to; nothing saved")
    if errors := _validate(root, new_text):
        raise _refused(errors)
    if reuse and (same := _same_pending(runs_dir, kind, old, new_text)) is not None:
        return same
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


def _plan_bind(
    registry: Mapping[str, Any],
    by_slot: Mapping[str, Mapping[str, Any]],
    slot: str,
    models: Sequence[str],
) -> tuple[Mapping[str, Any], str | list[str]]:
    """The slot's row and the value that binding `models` would give it, or why not."""
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
    return row, (models if is_list else models[0])


def _set_slot(expected: dict[str, Any], slot: str, value: str | list[str]) -> None:
    node = expected["agents"]
    path = slot.split(".")
    for key in path[:-1]:
        node = node[key]
    node[path[-1]] = value


def _egress_notes(
    registry: Mapping[str, Any], row: Mapping[str, Any], models: Sequence[str]
) -> list[str]:
    return [
        f"{model} runs through {registry['models'][model]['provider']}: what {row['agent']} is "
        "sent leaves this host (policy/data_classes.yaml still decides what may go)."
        for model in models
        if backend_location(registry, model) != "local"
    ]


def draft_bind(root: Path, runs_dir: Path, slot: str, models: Sequence[str]) -> Draft:
    """Bind `slot` (e.g. `design.proposal_b.primary`) to `models`, a list for a list slot."""
    old = _registry_bytes(root)
    registry = _load(old)
    row, value = _plan_bind(registry, {s["slot"]: s for s in slots(registry)}, slot, models)
    if value == row["value"]:
        raise ChangeError(f"{slot} is already {value}")
    expected = copy.deepcopy(registry)
    _set_slot(expected, slot, value)
    try:
        new_text = registry_edit.set_slot(old.decode("utf-8"), slot.split("."), value)
    except registry_edit.EditError as exc:
        raise ChangeError(str(exc)) from exc
    notes = _egress_notes(registry, row, [value] if isinstance(value, str) else value)
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


def draft_bind_many(
    root: Path,
    runs_dir: Path,
    bindings: Sequence[tuple[str, Sequence[str]]],
    *,
    origin: str,
    reasons: Mapping[str, str] | None = None,
) -> Draft:
    """Bind several slots at once, as one draft (the authority's proposal).

    A binding that names a slot's current value is left out; a proposal that changes nothing,
    names a slot twice, or breaks any rule is refused whole, so a draft is all or nothing.
    `reasons` is the proposer's one-line explanation per slot, shown with the diff.
    """
    old = _registry_bytes(root)
    registry = _load(old)
    by_slot = {s["slot"]: s for s in slots(registry)}
    seen = [slot for slot, _ in bindings]
    if len(set(seen)) != len(seen):
        raise ChangeError(
            f"the proposal names a slot twice: {sorted(s for s in seen if seen.count(s) > 1)}"
        )
    text, expected = old.decode("utf-8"), copy.deepcopy(registry)
    notes: list[str] = []
    changed: list[str] = []
    for slot, models in bindings:
        row, value = _plan_bind(registry, by_slot, slot, models)
        if value == row["value"]:
            continue
        _set_slot(expected, slot, value)
        try:
            text = registry_edit.set_slot(text, slot.split("."), value)
        except registry_edit.EditError as exc:
            raise ChangeError(str(exc)) from exc
        changed.append(slot)
        line = f"{slot}: {row['value']} -> {value}"
        if row["operations"]:
            line += f" (serves {', '.join(row['operations'])})"
        notes.append(line)
        if reasons and slot in reasons:
            notes.append(f"Why ({origin}): {reasons[slot]}")
        notes += _egress_notes(registry, row, [value] if isinstance(value, str) else value)
    if not changed:
        raise ChangeError("the proposal changes nothing: every slot it names already has that")
    return _finish(
        root,
        runs_dir,
        kind="bind_many",
        summary=f"Bind {len(changed)} slot{'s' * (len(changed) != 1)} ({origin})",
        notes=notes,
        old=old,
        new_text=text,
        expected=expected,
    )


# ── Agents that have no model ────────────────────────────────────────────────


class MappingIncomplete(ChangeError):
    """Some agent has no model, so no run is started."""


def gaps(root: Path) -> list[Gap]:
    """The agents that have no model (registry rule R20); none if the registry cannot be read,
    which the repository's own validation reports."""
    try:
        return spec_gaps(_load(_registry_bytes(root)), root)
    except (ChangeError, KeyError, TypeError, AttributeError):
        return []


def refuse_start(found: Sequence[Gap]) -> MappingIncomplete:
    count = f"{len(found)} agents have" if len(found) != 1 else "1 agent has"
    return MappingIncomplete(
        f"{count} no model ({', '.join(g.spec for g in found)}), so no run is started: ask for "
        "complete_mapping (/start proposes the mapping), confirm it, then start again"
    )


def draft_fill_gaps(
    root: Path, runs_dir: Path, choices: Mapping[str, str], *, origin: str
) -> Draft:
    """Give every agent that has no model one, as a single draft.

    `choices` maps each gap's agent to a catalog model. One draft has to close every gap,
    because the whole repository is validated with it. A model never becomes a primary here:
    an entry that exists gets the model as `model_assistance` (its `primary` stays what a human
    wrote: `human_authority` is a human, `exception_router` is deterministic), and a spec no
    entry owns gets a new entry, `{spec, primary}`, that has no stage wiring and serves nothing.
    """
    old = _registry_bytes(root)
    registry = _load(old)
    found = spec_gaps(registry, root)
    if not found:
        raise ChangeError("every agent already has a model")
    for key in {g.agent for g in found if g.kind == "no_entry"}:
        files = [g.spec for g in found if g.kind == "no_entry" and g.agent == key]
        if len(files) > 1:
            raise ChangeError(
                f"{' and '.join(files)} would both be agents.{key}; rename one of the files"
            )
    text, expected = old.decode("utf-8"), copy.deepcopy(registry)
    notes: list[str] = []
    for gap in found:
        model = choices.get(gap.agent)
        if model is None:
            raise ChangeError(f"no model was chosen for {gap.agent} ({gap.spec})")
        if model not in registry["models"]:
            raise ChangeError(f"{model!r} is not in the catalog")
        try:
            if gap.kind == "no_entry":
                if gap.agent in registry["agents"]:
                    raise ChangeError(
                        f"agents.{gap.agent} already exists with another spec, so {gap.spec} "
                        "cannot be given that name; add its entry by hand"
                    )
                text = registry_edit.add_agent(text, gap.agent, gap.spec, model)
                expected["agents"][gap.agent] = {"spec": gap.spec, "primary": model}
                notes.append(
                    f"agents.{gap.agent} is new: it owns {gap.spec} and runs on {model}. It has "
                    "no stage wiring, so it serves no operation until a human declares one."
                )
            else:
                entry = expected["agents"][gap.agent]
                have = entry.get("model_assistance", [])
                value = [*([have] if isinstance(have, str) else have), model]
                if "model_assistance" in entry:
                    text = registry_edit.set_slot(text, [gap.agent, "model_assistance"], value)
                else:
                    text = registry_edit.add_key(
                        text, ["agents", gap.agent], "model_assistance", value
                    )
                entry["model_assistance"] = value
                notes.append(
                    f"agents.{gap.agent} had no model: {model} now assists it "
                    "(model_assistance). Its primary is untouched."
                )
        except registry_edit.EditError as exc:
            raise ChangeError(str(exc)) from exc
        notes += _egress_notes(registry, {"agent": gap.agent}, [model])
    return _finish(
        root,
        runs_dir,
        kind="fill_gaps",
        summary=f"Give {len(found)} agent{'s' * (len(found) != 1)} a model ({origin})",
        notes=notes,
        old=old,
        new_text=text,
        expected=expected,
        reuse=True,
    )


def propose_fill(root: Path, runs_dir: Path) -> Draft:
    """The draft code makes when agents have no model: the coordinator named in `LN-AGENTS.md`
    for each of them. No model chose it. Refused if there is no coordinator to give."""
    from agentic_pipeline.coordinator import SETUP_FILE, SetupError, load_setup

    registry = _load(_registry_bytes(root))
    try:
        setup = load_setup(root)
    except SetupError as exc:
        raise ChangeError(f"{SETUP_FILE} cannot be used: {exc}") from exc
    named = [m for m in (setup.coordinator, setup.fallback) if m] if setup else []
    if not named:
        raise ChangeError(
            f"{SETUP_FILE} names no coordinator. Set `coordinator:` in its front matter to a "
            "catalog model: ln-agents gives that model to each agent that has none, and "
            "/agents changes any binding afterwards"
        )
    model = named[0]  # the coordinator itself: a missing one is not swapped for the fallback
    if model not in registry["models"]:
        raise ChangeError(
            f"the coordinator {model!r} in {SETUP_FILE} is not in the catalog "
            f"({', '.join(sorted(registry['models']))}); name one that is"
        )
    found = spec_gaps(registry, root)
    return draft_fill_gaps(
        root,
        runs_dir,
        {gap.agent: model for gap in found},
        origin=f"by code, from the coordinator in {SETUP_FILE}: no model chose this",
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
        raise _refused(errors)
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
