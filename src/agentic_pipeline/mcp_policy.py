"""What the local project MCP server may do, decided by code and never by a model.

`python -m agentic_pipeline serve mcp --profile P` offers one agent session the tools of
profile P in `policy/tools.yaml` and nothing else. This module is the part that needs no MCP
SDK: loading and checking that policy, the rules for reading the project's files, the secret
scan, and the audit log. `mcp_server.py` wires it to the SDK.

* A profile names a destination (where the caller's model runs, in the kinds of
  `policy/data_classes.yaml`). Repository content, and the run documents that can carry
  code, are a class of their own in that file (`artifact_defaults.pinned_source_tree`); a
  destination with `cloud: true` receives them only if that class is `cloud_allowed`. By
  default it is `confidential`, so a cloud profile gets no repository content at all.
* Files are read through one confinement: no absolute path, no `..`, no backslash, no symlink
  that leaves the project root, nothing under a directory that holds secrets or run data
  (`UNREADABLE_DIRS`), nothing named like a credential file (`SECRET_NAMES`). Text only, in
  slices of at most the profile's `max_read_bytes`. A result that matches a secret pattern
  (`build_gates.SECRET_PATTERNS`) is withheld.
* Every call, allowed or denied, is appended to a hash-chained log under `<runs>/.mcp/`.
  If the log cannot be written the call is denied: no unaudited access.

These are safe defaults that use only policy that already exists. Path-level data classes and
which agent gets which profile are planned for every agent together, later.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import re
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any

from agentic_pipeline.build_gates import SECRET_PATTERNS
from agentic_pipeline.graph import loads_yaml
from agentic_pipeline.grounding import PINNED_SOURCES
from agentic_pipeline.manifest import SealedInput
from agentic_pipeline.provenance import JsonlProvenanceLog, ProvenanceError
from agentic_pipeline.registry import _backend_refs, backend_location, op_backends, operations
from agentic_pipeline.schemas import ValidationError, validate

TOOLS_FILE = "policy/tools.yaml"
AUDIT_DIR = ".mcp"
AUDIT_RUN = "mcp"
AUDIT_STATE = "MCP"  # the `state` of the audit log's events

REPO_TOOLS = frozenset({"repo_list", "repo_read", "repo_search"})
RUN_TOOLS = frozenset({"run_status", "run_evidence"})
PIPELINE_TOOLS = frozenset({"pipeline_guideline", "models"})
# The tools the code implements. schemas/tools_policy.schema.json lists exactly these.
IMPLEMENTED_TOOLS = REPO_TOOLS | RUN_TOOLS | PIPELINE_TOOLS
# Tools that return repository content, or run documents that can carry it (an implementation
# patch, for one). They need the class of the source tree to be allowed at the destination.
CONTENT_TOOLS = REPO_TOOLS | {"run_evidence"}

UNREADABLE_DIRS = frozenset(
    {".git", ".runs", ".ssh", ".gnupg", ".aws", ".venv", "node_modules", "__pycache__", "target"}
)
SECRET_NAMES = re.compile(
    r"^(\.env(\..*)?|\.netrc|\.npmrc|\.pypirc|id_(rsa|dsa|ecdsa|ed25519).*|credentials(\..*)?"
    r"|.*\.(pem|key|p12|pfx|jks|keystore)|.*secrets?.*\.(ya?ml|json|toml|env))$",
    re.IGNORECASE,
)
MAX_LIST_ENTRIES = 500
MAX_SEARCH_RESULTS = 200
MAX_SEARCH_FILES = 5000
MAX_SEARCH_BYTES = 16 * 1024 * 1024
MAX_SEARCH_FILE_BYTES = 1024 * 1024
MAX_QUERY = 200
MAX_LINE = 300
RUN_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")


class ToolsPolicyError(Exception):
    """policy/tools.yaml cannot be used; `str(exc)` lists why."""


class Denied(Exception):
    """A call the policy refuses. `code` is the reason code the audit log records."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class Limits:
    max_read_bytes: int
    max_calls: int


@dataclass(frozen=True, slots=True)
class Profile:
    name: str
    description: str
    destination: str
    tools: tuple[str, ...]
    limits: Limits


@dataclass(frozen=True, slots=True)
class ToolsPolicy:
    digest: str
    profiles: Mapping[str, Profile]


# ── The policy file ──────────────────────────────────────────────────────────


def destination_is_cloud(data_classes: Mapping[str, Any], destination: str) -> bool:
    """Whether the destination is outside the host. A kind the file does not list is
    undetermined, never clear."""
    entry = (data_classes.get("destinations") or {}).get(destination)
    return not isinstance(entry, Mapping) or entry.get("cloud") is not False


def content_allowed(data_classes: Mapping[str, Any], destination: str) -> bool:
    """Whether repository content may reach a caller at `destination`."""
    if not destination_is_cloud(data_classes, destination):
        return True
    tree = (data_classes.get("artifact_defaults") or {}).get("pinned_source_tree", "unknown")
    entry = (data_classes.get("classes") or {}).get(tree)
    return isinstance(entry, Mapping) and entry.get("cloud_allowed") is True


def check_tools_policy(doc: Any, data_classes: Mapping[str, Any]) -> list[str]:
    """Every problem in a parsed policy/tools.yaml (empty means usable)."""
    try:
        validate("tools_policy", doc)
    except ValidationError as exc:
        return [f"tools: {exc.message}"]
    errors = []
    unimplemented = {t for p in doc["profiles"].values() for t in p["tools"]} - IMPLEMENTED_TOOLS
    if unimplemented:
        errors.append(f"tools: no code behind {sorted(unimplemented)}")
    destinations = data_classes.get("destinations") or {}
    for name, profile in doc["profiles"].items():
        if profile["destination"] not in destinations:
            errors.append(
                f"tools.{name}: destination {profile['destination']!r} is not one of "
                f"policy/data_classes.yaml destinations {sorted(destinations)}"
            )
    return errors


def _outside_host(registry: Mapping[str, Any], backend: str) -> bool:
    """Only a provider declared `local` stays on the host; a missing or odd value is outside."""
    return backend_location(registry, backend) != "local"


def check_operation_tools(
    registry: Mapping[str, Any], tools: Mapping[str, Any] | None, data_classes: Mapping[str, Any]
) -> list[str]:
    """What the registry's `tool_profile` and `tool_reads` promise, checked against
    policy/tools.yaml and the data classes (empty means they add up). `tools` is None when
    the policy has no tools.yaml.

    * the profile exists, and its destination is at least as strict as every model the operation
      (or agent) may run on: a model outside the host needs a destination outside it, so the
      class gate always applies;
    * `tool_reads` name sealed catalog artifacts that the operation does not also deny, does not
      produce and does not take from another operation of its own stage (design independence);
    * repository tools and `tool_reads` go together, and a destination outside the host has at
      least one artifact it may receive when the classes are known;
    * an operation with session separation (the design stages) is given no run tools.
    """
    profiles = (tools or {}).get("profiles") or {}
    catalog = registry.get("artifacts") or {}
    classes = data_classes.get("classes") or {}
    defaults = data_classes.get("artifact_defaults") or {}
    isolation = registry.get("isolation_profiles") or {}
    errors: list[str] = []

    def profile_of(where: str, name: Any) -> Mapping[str, Any] | None:
        if tools is None:
            errors.append(f"tools: {where} names profile {name!r} but there is no {TOOLS_FILE}")
            return None
        if not isinstance(name, str) or name not in profiles:
            errors.append(
                f"tools: {where} names profile {name!r}, which {TOOLS_FILE} does not have "
                f"({', '.join(sorted(profiles)) or 'no profiles'})"
            )
            return None
        return profiles[name]

    def strict_enough(
        where: str, name: str, profile: Mapping[str, Any], backends: list[str]
    ) -> None:
        if destination_is_cloud(data_classes, profile["destination"]):
            return
        for backend in backends:
            if _outside_host(registry, backend):
                errors.append(
                    f"tools: {where} can run on {backend}, outside the host, but profile "
                    f"{name} has destination {profile['destination']}, inside it"
                )

    for stage, op in operations(registry.get("stage_requirements") or {}):
        name = op.get("tool_profile")
        if name is None:
            continue
        where = f"{stage}.{op['id']}"
        profile = profile_of(where, name)
        if profile is None:
            continue
        strict_enough(where, name, profile, op_backends(registry, stage, op))
        reads = list(op.get("tool_reads") or [])
        for artifact in reads:
            spec = catalog.get(artifact)
            producers = (spec or {}).get("producer") or []
            if spec is None or not spec.get("sealed"):
                errors.append(
                    f"tools: {where} tool_reads {artifact!r}: not a sealed catalog artifact"
                )
            elif artifact in (op.get("denied_reads") or []):
                errors.append(f"tools: {where} tool_reads {artifact!r} is also in denied_reads")
            elif where in producers:
                errors.append(f"tools: {where} tool_reads {artifact!r}, which it produces")
            elif any(x.startswith(f"{stage}.") for x in producers):
                errors.append(
                    f"tools: {where} tool_reads {artifact!r}, "
                    f"produced by another operation of {stage}"
                )
        repo = sorted(set(profile["tools"]) & REPO_TOOLS)
        if repo and not reads:
            errors.append(f"tools: {where} has repository tools {repo} but no tool_reads to serve")
        if reads and not repo:
            errors.append(
                f"tools: {where} has tool_reads but profile {name} lists no repository tool"
            )
        if repo and reads and destination_is_cloud(data_classes, profile["destination"]):
            known = [defaults[a] for a in reads if a in defaults]
            if len(known) == len(reads) and not any(
                (classes.get(c) or {}).get("cloud_allowed") is True for c in known
            ):
                errors.append(
                    f"tools: {where} tool_reads {reads} are all of classes {sorted(set(known))}, "
                    f"which a {profile['destination']} destination may not receive"
                )
        run_tools = sorted(set(profile["tools"]) & RUN_TOOLS)
        if run_tools and "session_separation" in (isolation.get(op.get("isolation_profile")) or []):
            errors.append(
                f"tools: {where} needs session separation, so profile {name} may not list "
                f"{run_tools}"
            )

    for agent_name, agent in (registry.get("agents") or {}).items():
        name = agent.get("tool_profile") if isinstance(agent, Mapping) else None
        if name is None:
            continue
        where = f"agents.{agent_name}"
        profile = profile_of(where, name)
        if profile is not None:
            refs = [ref for _, ref in _backend_refs(agent, where)]
            strict_enough(where, name, profile, refs)
    return errors


def parse_tools_policy(data: bytes, data_classes: Mapping[str, Any]) -> ToolsPolicy:
    """The checked policy, or `ToolsPolicyError`."""
    try:
        doc = loads_yaml(data)
    except Exception as exc:
        raise ToolsPolicyError(f"{TOOLS_FILE} is not readable YAML: {exc}") from exc
    if errors := check_tools_policy(doc, data_classes):
        raise ToolsPolicyError("; ".join(errors))
    profiles = {
        name: Profile(
            name,
            p.get("description", ""),
            p["destination"],
            tuple(p["tools"]),
            Limits(p["limits"]["max_read_bytes"], p["limits"]["max_calls"]),
        )
        for name, p in doc["profiles"].items()
    }
    return ToolsPolicy("sha256:" + hashlib.sha256(data).hexdigest(), profiles)


def load_data_classes(root: Path) -> Mapping[str, Any]:
    try:
        doc = loads_yaml((root / "policy" / "data_classes.yaml").read_bytes())
    except (OSError, ValueError) as exc:
        raise ToolsPolicyError(f"cannot read policy/data_classes.yaml in {root}: {exc}") from exc
    if not isinstance(doc, Mapping):
        raise ToolsPolicyError("policy/data_classes.yaml: top level must be a mapping")
    return doc


def load_tools_policy(root: Path, data_classes: Mapping[str, Any] | None = None) -> ToolsPolicy:
    classes = data_classes if data_classes is not None else load_data_classes(root)
    try:
        data = (root / TOOLS_FILE).read_bytes()
    except OSError as exc:
        raise ToolsPolicyError(f"cannot read {TOOLS_FILE} in {root}: {exc}") from exc
    return parse_tools_policy(data, classes)


def offered_tools(
    profile: Profile, data_classes: Mapping[str, Any], view: View | None = None
) -> tuple[str, ...]:
    """The tools a profile gets, among those it lists: a repository tool only if the view holds
    some file the destination may receive, and `run_evidence` only if the destination may
    receive repository content (a saved report can carry code)."""

    def usable(tool: str) -> bool:
        if tool in REPO_TOOLS:
            return view is not None and view.any_allowed()
        if tool == "run_evidence":
            return content_allowed(data_classes, profile.destination)
        return True

    return tuple(t for t in profile.tools if usable(t))


def withheld_tools(profile: Profile, offered: Sequence[str]) -> tuple[str, ...]:
    return tuple(t for t in profile.tools if t not in offered)


# ── Reading the project ──────────────────────────────────────────────────────


def _unreadable(part: str) -> bool:
    return part in UNREADABLE_DIRS or bool(SECRET_NAMES.match(part))


def relative_parts(rel: str) -> tuple[str, ...]:
    """The parts of a project-relative path, or `Denied`. Forward slashes only."""
    if "\x00" in rel or "\\" in rel:
        raise Denied("bad_path", "a path has no NUL or backslash; use forward slashes")
    if rel.startswith("/") or re.match(r"^[A-Za-z]:", rel):
        raise Denied("path_outside_root", "a path is relative to the project root")
    parts = tuple(p for p in PurePosixPath(rel or ".").parts if p != ".")
    if ".." in parts:
        raise Denied("path_outside_root", "a path stays inside the project root: no `..`")
    if any(_unreadable(p) for p in parts):
        raise Denied("unreadable_path", f"{rel}: this path is not readable through MCP")
    return parts


def resolve_readable(root: Path, rel: str) -> Path:
    """The real path of `rel` inside `root`, or `Denied`. Symlinks are followed and the
    result must still be inside the root and still readable."""
    base = root.resolve()
    target = base.joinpath(*relative_parts(rel))
    real = target.resolve()
    if not real.is_relative_to(base):
        raise Denied("path_outside_root", f"{rel}: resolves outside the project root")
    if any(_unreadable(p) for p in real.relative_to(base).parts):
        raise Denied("unreadable_path", f"{rel}: this path is not readable through MCP")
    if not real.exists():
        raise Denied("not_found", f"{rel}: no such file or directory")
    return real


def scan_secrets(text: str) -> list[str]:
    """The kinds of secret that `text` matches."""
    return [kind for kind, rx in SECRET_PATTERNS.items() if rx.search(text)]


def list_dir(
    root: Path, rel: str, hide: frozenset[tuple[str, ...]] = frozenset()
) -> dict[str, Any]:
    """The readable entries of a directory. `hide` holds paths (relative to `root`) that another
    mount serves, so they are not listed here."""
    real = resolve_readable(root, rel)
    if not real.is_dir():
        raise Denied("not_a_directory", f"{rel}: not a directory")
    base, entries, truncated = root.resolve(), [], False
    here = relative_parts(rel)
    for child in sorted(real.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
        if _unreadable(child.name) or (*here, child.name) in hide:
            continue
        try:
            resolved = child.resolve()
        except OSError:
            continue
        if not resolved.is_relative_to(base) or any(
            _unreadable(p) for p in resolved.relative_to(base).parts
        ):
            continue
        if len(entries) >= MAX_LIST_ENTRIES:
            truncated = True
            break
        is_dir = resolved.is_dir()
        entries.append(
            {
                "name": child.name,
                "kind": "dir" if is_dir else "file",
                "size": None if is_dir else resolved.stat().st_size,
            }
        )
    return {
        "path": "/".join(relative_parts(rel)) or ".",
        "entries": entries,
        "truncated": truncated,
    }


def read_text(root: Path, rel: str, offset: int, length: int | None, limit: int) -> dict[str, Any]:
    """A slice of a text file, at most `limit` bytes; `Denied` for a binary file or a secret."""
    if offset < 0 or (length is not None and length < 1):
        raise Denied("bad_argument", "offset is 0 or more and length is 1 or more")
    real = resolve_readable(root, rel)
    if not real.is_file():
        raise Denied("not_a_file", f"{rel}: not a file")
    size = real.stat().st_size
    want = min(length or limit, limit)
    with real.open("rb") as fh:
        fh.seek(offset)
        data = fh.read(want)
    if b"\x00" in data:
        raise Denied("not_text", f"{rel}: binary file")
    text = data.decode("utf-8", errors="replace")
    if kinds := scan_secrets(text):
        raise Denied("secret_in_content", f"{rel}: withheld, it matches {', '.join(kinds)}")
    return {
        "path": "/".join(relative_parts(rel)),
        "size": size,
        "offset": offset,
        "length": len(data),
        "text": text,
        "truncated": offset + len(data) < size,
    }


def search_text(
    root: Path,
    query: str,
    rel: str,
    glob: str | None,
    ignore_case: bool,
    max_results: int,
    limit: int,
    hide: frozenset[tuple[str, ...]] = frozenset(),
    prefix: tuple[str, ...] = (),
) -> dict[str, Any]:
    """Lines containing `query` in the readable text files under `rel`. `hide` holds paths
    (relative to `root`) that another mount serves; `prefix` is where `root` appears in the
    project, and goes in front of the paths returned."""
    if not query or len(query) > MAX_QUERY:
        raise Denied("bad_argument", f"the query is 1 to {MAX_QUERY} characters")
    start = resolve_readable(root, rel)
    base = root.resolve()
    needle = query.lower() if ignore_case else query
    results: list[dict[str, Any]] = []
    scanned = scanned_bytes = withheld = used = 0
    stopped = ""

    def candidates() -> Sequence[Path]:
        if start.is_file():
            return [start]
        found: list[Path] = []
        for dirpath, dirs, files in os.walk(start, followlinks=False):
            at = Path(dirpath).relative_to(base).parts
            dirs[:] = sorted(d for d in dirs if not _unreadable(d) and (*at, d) not in hide)
            found += [Path(dirpath, f) for f in sorted(files) if not _unreadable(f)]
        return found

    for path in candidates():
        try:
            real = path.resolve()
            parts = real.relative_to(base).parts
        except (OSError, ValueError):
            continue
        if any(_unreadable(p) for p in parts):
            continue
        shown = (
            "/".join((*prefix, *path.relative_to(base).parts)) if path.is_relative_to(base) else ""
        )
        if glob and not fnmatch.fnmatch(shown, glob):
            continue
        if scanned >= MAX_SEARCH_FILES or scanned_bytes >= MAX_SEARCH_BYTES:
            stopped = "scan limit"
            break
        try:
            size = real.stat().st_size
            if size > MAX_SEARCH_FILE_BYTES:
                continue
            data = real.read_bytes()
        except OSError:
            continue
        scanned += 1
        scanned_bytes += len(data)
        if b"\x00" in data[:8192]:
            continue
        for number, line in enumerate(data.decode("utf-8", errors="replace").splitlines(), 1):
            if needle not in (line.lower() if ignore_case else line):
                continue
            snippet = line.strip()[:MAX_LINE]
            if scan_secrets(snippet):
                withheld += 1
                continue
            cost = len(shown) + len(snippet) + 32
            if len(results) >= max_results or used + cost > limit:
                stopped = "result limit"
                break
            used += cost
            results.append({"path": shown, "line": number, "text": snippet})
        if stopped:
            break
    return {
        "query": query,
        "matches": results,
        "files_scanned": scanned,
        "withheld_matches": withheld,
        "truncated": bool(stopped),
        "stopped_by": stopped or None,
    }


# ── What a session can see: mounts of sealed or live trees ────────────────────


@dataclass(frozen=True, slots=True)
class Mount:
    """A tree that appears in the project at `prefix`, with the one class its files have."""

    prefix: tuple[str, ...]
    path: Path
    data_class: str
    label: str  # "project" for the live checkout, else the sealed artifact's id


@dataclass(frozen=True, slots=True)
class View:
    """The files a session can reach, as mounts. A path belongs to the mount with the longest
    matching prefix, so a narrower artifact (the contracts, `internal`) refines a broader one
    (the whole tree, `confidential`) where they overlap. `cloud` says the caller's model is
    outside the host: then only files of a `cloud_allowed` class are returned."""

    mounts: tuple[Mount, ...]
    cloud: bool
    classes: Mapping[str, Any]

    def allowed(self, data_class: str) -> bool:
        if not self.cloud:
            return True
        entry = self.classes.get(data_class)
        return isinstance(entry, Mapping) and entry.get("cloud_allowed") is True

    def any_allowed(self) -> bool:
        return any(self.allowed(m.data_class) for m in self.mounts)

    def locate(self, parts: tuple[str, ...]) -> tuple[Mount, tuple[str, ...]] | None:
        best = None
        for m in self.mounts:
            if parts[: len(m.prefix)] == m.prefix and (
                best is None or len(m.prefix) > len(best.prefix)
            ):
                best = m
        return (best, parts[len(best.prefix) :]) if best else None

    def hidden_under(self, mount: Mount) -> frozenset[tuple[str, ...]]:
        """Paths inside `mount` (relative to it) that a narrower mount serves instead."""
        n = len(mount.prefix)
        return frozenset(
            m.prefix[n:]
            for m in self.mounts
            if m is not mount and len(m.prefix) > n and m.prefix[:n] == mount.prefix
        )

    @classmethod
    def live(cls, root: Path, data_class: str, cloud: bool, classes: Mapping[str, Any]) -> View:
        """The project's working tree as one mount; its class is the source tree's."""
        return cls((Mount((), root.resolve(), data_class, "project"),), cloud, classes)


def tree_class(data_classes: Mapping[str, Any]) -> str:
    return (data_classes.get("artifact_defaults") or {}).get("pinned_source_tree", "unknown")


def snapshot_view(
    found: Mapping[str, SealedInput], cloud: bool, classes: Mapping[str, Any]
) -> View:
    """Sealed artifacts as a view. The pinned ones appear where they sit in the repository
    (`grounding.PINNED_SOURCES`: the tree at the root, the contracts under `contracts`, the
    tests under `tests`); any other artifact appears under a directory named by its id."""
    mounts = []
    for artifact, sealed in found.items():
        where = PINNED_SOURCES.get(artifact, artifact)
        prefix = tuple(PurePosixPath(where).parts) if where else ()
        mounts.append(Mount(prefix, sealed.path.resolve(), sealed.data_class, artifact))
    return View(tuple(mounts), cloud, classes)


def _denied_class(view: View, mount: Mount, rel: str) -> Denied:
    return Denied(
        "class_not_allowed",
        f"{rel or '.'}: its class ({mount.data_class}) may not be sent to a caller outside "
        "the host",
    )


def _joined(prefix: tuple[str, ...], rel: str) -> str:
    return "/".join((*prefix, *relative_parts(rel))) or "."


def view_list(view: View, rel: str) -> dict[str, Any]:
    parts = relative_parts(rel)
    entries: dict[str, dict[str, Any]] = {}
    truncated = False
    located = view.locate(parts)
    if located is not None and view.allowed(located[0].data_class):
        mount, rest = located
        listing = list_dir(mount.path, "/".join(rest) or ".", view.hidden_under(mount))
        entries.update({e["name"]: e for e in listing["entries"]})
        truncated = listing["truncated"]
    for m in view.mounts:  # directories that exist only because a narrower mount sits below
        if (
            len(m.prefix) > len(parts)
            and m.prefix[: len(parts)] == parts
            and view.allowed(m.data_class)
        ):
            entries.setdefault(
                m.prefix[len(parts)], {"name": m.prefix[len(parts)], "kind": "dir", "size": None}
            )
    if not entries:
        if located is not None and not view.allowed(located[0].data_class):
            raise _denied_class(view, located[0], rel)
        if located is None:
            raise Denied("not_found", f"{rel}: no such file or directory")
    ordered = sorted(entries.values(), key=lambda e: (e["kind"] != "dir", e["name"].lower()))
    return {
        "path": "/".join(parts) or ".",
        "entries": ordered[:MAX_LIST_ENTRIES],
        "truncated": truncated or len(ordered) > MAX_LIST_ENTRIES,
    }


def view_read(view: View, rel: str, offset: int, length: int | None, limit: int) -> dict[str, Any]:
    parts = relative_parts(rel)
    located = view.locate(parts)
    if located is None:
        raise Denied("not_found", f"{rel}: no such file or directory")
    mount, rest = located
    if not view.allowed(mount.data_class):
        raise _denied_class(view, mount, rel)
    result = read_text(mount.path, "/".join(rest) or ".", offset, length, limit)
    return {**result, "path": "/".join(parts)}


def view_search(
    view: View,
    query: str,
    rel: str,
    glob: str | None,
    ignore_case: bool,
    max_results: int,
    limit: int,
) -> dict[str, Any]:
    if not query or len(query) > MAX_QUERY:
        raise Denied("bad_argument", f"the query is 1 to {MAX_QUERY} characters")
    target = relative_parts(rel)
    matches: list[dict[str, Any]] = []
    scanned = withheld = used = 0
    stopped: str | None = None
    refused = searched = 0
    for mount in view.mounts:
        if target[: len(mount.prefix)] == mount.prefix:
            inside = target[len(mount.prefix) :]
        elif mount.prefix[: len(target)] == target:
            inside = ()
        else:
            continue
        hidden = view.hidden_under(mount)
        if any(inside[: len(h)] == h for h in hidden):
            continue  # a narrower mount serves this part
        if not view.allowed(mount.data_class):
            refused += 1
            continue
        searched += 1
        found = search_text(
            mount.path,
            query,
            "/".join(inside) or ".",
            glob,
            ignore_case,
            max_results - len(matches),
            max(0, limit - used),
            hidden,
            mount.prefix,
        )
        matches += found["matches"]
        used += sum(len(m["path"]) + len(m["text"]) + 32 for m in found["matches"])
        scanned += found["files_scanned"]
        withheld += found["withheld_matches"]
        stopped = stopped or found["stopped_by"]
        if len(matches) >= max_results:
            stopped = stopped or "result limit"
            break
    if not searched:
        if refused:
            raise Denied(
                "class_not_allowed",
                f"{rel or '.'}: its class may not be sent to a caller outside the host",
            )
        raise Denied("not_found", f"{rel}: no such file or directory")
    return {
        "query": query,
        "matches": matches,
        "files_scanned": scanned,
        "withheld_matches": withheld,
        "truncated": bool(stopped),
        "stopped_by": stopped,
    }


def run_id(value: str) -> str:
    if not RUN_ID.fullmatch(value):
        raise Denied("bad_argument", "a run id is letters, digits, _ and - only")
    return value


# ── The session and its audit log ────────────────────────────────────────────


def arguments_hash(arguments: Mapping[str, Any]) -> str:
    blob = json.dumps(arguments, sort_keys=True, default=str, ensure_ascii=False)
    return "sha256:" + hashlib.sha256(blob.encode("utf-8")).hexdigest()


class Session:
    """One server process: its profile, its call count and its audit log."""

    def __init__(
        self,
        profile: Profile,
        policy_digest: str,
        runs_dir: Path,
        offered: Sequence[str],
        session_id: str | None = None,
        scope: Mapping[str, Any] | None = None,
    ) -> None:
        self.profile = profile
        self.scope = dict(scope or {})  # recorded with every decision: the run, operation, ...
        self.policy_digest = policy_digest
        self.offered = tuple(offered)
        self.id = session_id or uuid.uuid4().hex[:12]
        self.calls = 0
        self.log = JsonlProvenanceLog(runs_dir / AUDIT_DIR, AUDIT_RUN)
        (runs_dir / AUDIT_DIR).mkdir(parents=True, exist_ok=True)

    def record(self, tool: str, arguments: Mapping[str, Any], allowed: bool, code: str) -> None:
        """Append one decision; raises `Denied("audit_unavailable")` if it cannot be written."""
        try:
            self.log.append(
                {
                    "state": AUDIT_STATE,
                    "event": "decision",
                    **self.scope,
                    "session": self.id,
                    "profile": self.profile.name,
                    "policy_digest": self.policy_digest,
                    "tool": tool,
                    "args_hash": arguments_hash(arguments),
                    "allowed": allowed,
                    "reason_code": code,
                }
            )
        except (OSError, ProvenanceError, ValueError) as exc:
            raise Denied("audit_unavailable", f"the audit log cannot be written: {exc}") from exc

    def start(self) -> None:
        withheld = withheld_tools(self.profile, self.offered)
        self.record(
            "(session)",
            {"offered": list(self.offered), "withheld": list(withheld)},
            True,
            "session_started",
        )

    def begin(self) -> None:
        """Count a call and refuse it past the profile's limit. The caller records the outcome."""
        self.calls += 1
        if self.calls > self.profile.limits.max_calls:
            raise Denied("call_limit", f"the profile allows {self.profile.limits.max_calls} calls")

    @property
    def read_limit(self) -> int:
        return self.profile.limits.max_read_bytes
