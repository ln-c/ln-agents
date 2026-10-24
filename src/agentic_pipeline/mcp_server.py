"""The local project MCP server (`ln-pipeline`): `python -m agentic_pipeline serve mcp`.

ln-agents is the MCP host. For one agent's session it starts this server over stdio with
`--profile P`; the server offers that agent the tools of profile P in `policy/tools.yaml` and
refuses everything else. It is local (the spawning host is its only caller) and it serves the
project it is started in, read-only: files through `repo_list`, `repo_read` and `repo_search`,
and the pipeline's own data through `run_status`, `run_evidence`, `pipeline_guideline` and
`models`. No tool approves, signs, decides, resumes or changes policy, budgets or vetoes: the
tool names are a closed list in `schemas/tools_policy.schema.json`.

What a caller may see is decided by code, in `mcp_policy.py`: a tool the profile does not list is
not registered; repository content is withheld from a destination that `policy/data_classes.yaml`
does not let receive it; paths are confined to the project root; secrets are withheld; every call,
allowed or denied, goes to a hash-chained log under `<runs>/.mcp/`. A model's own tool list,
description or request grants nothing.

Needs the `mcp` extra (`uv sync --extra mcp`).
"""

from __future__ import annotations

import functools
import json
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from mcp.server import MCPServer
from mcp.server.context import ServerRequestContext
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations

from agentic_pipeline import guideline, mcp_policy, registry_changes
from agentic_pipeline.mcp_policy import Denied, Session, ToolsPolicy, ToolsPolicyError
from agentic_pipeline.models_cli import CatalogError, Deps, catalog_rows
from agentic_pipeline.readiness import run_preflight_tolerating_gaps
from agentic_pipeline.run_state import RunStateError, RunStore

SERVER_NAME = "ln-pipeline"
READ_ONLY = ToolAnnotations(
    readOnlyHint=True, destructiveHint=False, idempotentHint=True, openWorldHint=False
)
SIZE_FACTOR = 4  # a result may be at most this many times the profile's max_read_bytes


class ProjectTools:
    """The tools, for one project root and one session. Every public method is a tool whose
    name is its method name; `Denied` is how the policy refuses."""

    def __init__(self, root: Path, runs_dir: Path, session: Session) -> None:
        self.root, self.runs_dir, self.session = root, runs_dir, session

    # ── the project's files ──────────────────────────────────────────────────

    def repo_list(self, path: str = ".") -> dict[str, Any]:
        """List the files and directories under a project-relative path (default: the root).
        Directories that hold secrets or run data, and credential files, are not shown."""
        return mcp_policy.list_dir(self.root, path)

    def repo_read(self, path: str, offset: int = 0, length: int | None = None) -> dict[str, Any]:
        """Read a slice of a text file by project-relative path. At most the profile's byte
        limit per call; page with `offset`. Binary files and files that match a secret
        pattern are refused."""
        return mcp_policy.read_text(self.root, path, offset, length, self.session.read_limit)

    def repo_search(
        self,
        query: str,
        path: str = ".",
        glob: str | None = None,
        ignore_case: bool = True,
        max_results: int = 50,
    ) -> dict[str, Any]:
        """Find lines containing a plain-text query in the text files under a path, optionally
        only files whose project-relative path matches a glob such as `src/*.py`."""
        limit = max(1, min(max_results, mcp_policy.MAX_SEARCH_RESULTS))
        return mcp_policy.search_text(
            self.root, query, path, glob, ignore_case, limit, self.session.read_limit
        )

    # ── the pipeline's data ──────────────────────────────────────────────────

    def _store(self, run_id: str) -> RunStore:
        return RunStore(self.runs_dir / mcp_policy.run_id(run_id))

    @staticmethod
    def _no_run(run_id: str, store: RunStore, exc: RunStateError) -> Denied:
        """Why a run cannot be read, without the host's paths in the words."""
        if not store.path.exists():
            return Denied("not_found", f"no run {run_id}")
        return Denied("run_unreadable", f"the state of run {run_id} cannot be read")

    def run_status(self, run_id: str) -> dict[str, Any]:
        """Where a run stands: node, status, pause cause, budget use and whether a runner holds
        it. Reads the run's state; changes nothing."""
        store = self._store(run_id)
        try:
            return {**store.load().to_dict(), "runner_active": store.is_locked()}
        except RunStateError as exc:
            raise self._no_run(run_id, store, exc) from exc

    def run_evidence(self, run_id: str) -> dict[str, Any]:
        """The evidence report saved for the run's current node, if a person generated one
        (`/evidence`). Nothing is generated or written here."""
        store = self._store(run_id)
        try:
            node = store.load().current
        except RunStateError as exc:
            raise self._no_run(run_id, store, exc) from exc
        report = store.run_root / "approvals" / f"{node}-evidence.md"
        if not report.is_file():
            raise Denied(
                "not_found",
                f"no evidence report is saved for {node}; a person generates it with /evidence",
            )
        raw = report.read_bytes()
        limit = self.session.read_limit
        text = raw[:limit].decode("utf-8", errors="replace")
        if kinds := mcp_policy.scan_secrets(text):
            raise Denied("secret_in_content", f"the report is withheld, it matches {kinds}")
        return {
            "run_id": run_id,
            "node": node,
            "report": f"approvals/{report.name}",
            "markdown": text,
            "truncated": len(raw) > limit,
        }

    def pipeline_guideline(self) -> dict[str, Any]:
        """Where each step from naming a coordinator to a first run stands for this project."""
        try:
            return guideline.build(self.root, self.runs_dir)
        except Exception as exc:  # the answer is the reason, not a fault of the server
            return {
                "ready": False,
                "steps": [],
                "notes": [],
                "errors": [f"{type(exc).__name__}: {exc}"],
            }

    def models(self) -> dict[str, Any]:
        """The model catalog of this project's registry, and the agents that have no model."""
        found = registry_changes.gaps(self.root)
        gaps = [{"kind": g.kind, "agent": g.agent, "spec": g.spec} for g in found]
        deps = Deps(preflight=run_preflight_tolerating_gaps) if found else Deps()
        try:
            return {"models": catalog_rows(self.root, deps), "gaps": gaps, "errors": []}
        except CatalogError as exc:
            return {"models": [], "gaps": gaps, "errors": list(exc.errors)}


def _guarded(tools: ProjectTools, name: str) -> Callable[..., Any]:
    """The tool `name` behind the policy: counted, audited, size-limited, and refused as a
    `ToolError` that says why."""
    fn = getattr(tools, name)
    session = tools.session

    @functools.wraps(fn)
    def call(**arguments: Any) -> Any:
        try:
            session.begin()
            result = fn(**arguments)
            size = len(json.dumps(result, default=str))
            if size > SIZE_FACTOR * session.read_limit:
                raise Denied("too_large", f"the result is {size} bytes; narrow the request")
            session.record(name, arguments, True, "ok")
            return result
        except Denied as exc:
            try:
                session.record(name, arguments, False, exc.code)
            except Denied:
                pass  # the refusal stands whether or not it could be logged
            raise ToolError(f"denied ({exc.code}): {exc}") from None

    return call


def build_server(
    root: Path,
    runs_dir: Path,
    profile_name: str,
    policy: ToolsPolicy | None = None,
    data_classes: Mapping[str, Any] | None = None,
) -> tuple[MCPServer, Session]:
    """The server for one profile, with only that profile's tools registered. Raises
    `ToolsPolicyError` if the policy cannot be used or the profile does not exist."""
    classes = data_classes if data_classes is not None else mcp_policy.load_data_classes(root)
    policy = policy if policy is not None else mcp_policy.load_tools_policy(root, classes)
    profile = policy.profiles.get(profile_name)
    if profile is None:
        raise ToolsPolicyError(
            f"no profile {profile_name!r} in {mcp_policy.TOOLS_FILE}; it has "
            f"{', '.join(sorted(policy.profiles))}"
        )
    offered = mcp_policy.offered_tools(profile, classes)
    session = Session(profile, policy.digest, runs_dir, offered)
    tools = ProjectTools(root, runs_dir, session)

    async def audit_unoffered(ctx: ServerRequestContext[Any, Any], call_next: Any) -> Any:
        """Log a call to a tool this profile does not offer; the SDK then refuses it."""
        if ctx.method == "tools/call" and isinstance(ctx.params, Mapping):
            name = str(ctx.params.get("name", ""))
            if name not in offered:
                try:
                    session.record(name, {}, False, "tool_not_offered")
                except Denied:
                    pass
        return await call_next(ctx)

    server = MCPServer(
        SERVER_NAME,
        instructions=(
            f"Local, read-only server for the project at {root.name} (profile {profile.name}, "
            f"destination {profile.destination}). Paths are relative to the project root. "
            "Secrets, run data and .git are not readable, and every call is logged."
        ),
        log_level="WARNING",
        middleware=[audit_unoffered],
    )
    for name in offered:
        server.add_tool(
            _guarded(tools, name),
            name=name,
            description=(getattr(tools, name).__doc__ or "").strip(),
            annotations=READ_ONLY,
        )
    return server, session


def serve(root: Path, runs_dir: Path, profile_name: str) -> None:
    """Run the server for one profile over stdio until the host closes it."""
    server, session = build_server(root, runs_dir, profile_name)
    session.start()
    server.run(transport="stdio")
