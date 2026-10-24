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
from agentic_pipeline.provenance import JsonlProvenanceLog, ProvenanceError
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
            continue
        if not content_allowed(data_classes, profile["destination"]):
            held = sorted(set(profile["tools"]) & CONTENT_TOOLS)
            if held:
                errors.append(
                    f"tools.{name}: {held} return repository content, which "
                    f"policy/data_classes.yaml does not let a {profile['destination']} "
                    "destination receive (artifact_defaults.pinned_source_tree)"
                )
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


def offered_tools(profile: Profile, data_classes: Mapping[str, Any]) -> tuple[str, ...]:
    """The tools a profile gets: those it lists that its destination may use."""
    allowed = content_allowed(data_classes, profile.destination)
    return tuple(t for t in profile.tools if allowed or t not in CONTENT_TOOLS)


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


def list_dir(root: Path, rel: str) -> dict[str, Any]:
    real = resolve_readable(root, rel)
    if not real.is_dir():
        raise Denied("not_a_directory", f"{rel}: not a directory")
    base, entries, truncated = root.resolve(), [], False
    for child in sorted(real.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower())):
        if _unreadable(child.name):
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
) -> dict[str, Any]:
    """Lines containing `query` in the readable text files under `rel`."""
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
            dirs[:] = sorted(d for d in dirs if not _unreadable(d))
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
        shown = "/".join(path.relative_to(base).parts) if path.is_relative_to(base) else ""
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
    ) -> None:
        self.profile = profile
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
        self.record("(session)", {"offered": list(self.offered)}, True, "session_started")

    def begin(self) -> None:
        """Count a call and refuse it past the profile's limit. The caller records the outcome."""
        self.calls += 1
        if self.calls > self.profile.limits.max_calls:
            raise Denied("call_limit", f"the profile allows {self.profile.limits.max_calls} calls")

    @property
    def read_limit(self) -> int:
        return self.profile.limits.max_read_bytes
