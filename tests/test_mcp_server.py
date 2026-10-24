"""The local project MCP server through the SDK's own client: what each profile is offered,
what is refused, and what is logged."""

from __future__ import annotations

import asyncio
import json
import shutil
import sys
from pathlib import Path
from typing import Any

import pytest
from test_grounding import SPEC, git

from agentic_pipeline import mcp_policy as mp
from agentic_pipeline import mcp_server
from agentic_pipeline.provenance import verify_provenance_chain
from agentic_pipeline.runner import init_run

mcp_client = pytest.importorskip("mcp.client")
from mcp.client import Client  # noqa: E402
from mcp.client.stdio import StdioServerParameters  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
AWS = "AKIA" + "ABCDEFGHIJKLMNOP"
READ_TOOLS = {"repo_list", "repo_read", "repo_search"}


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    root.mkdir()
    for name in ("pipeline", "policy", "schemas", "agents"):
        shutil.copytree(ROOT / name, root / name)
    (root / "tests").mkdir()
    (root / "tests" / "test_x.py").write_text("def test_x(): ...\n")
    (root / "src").mkdir()
    (root / "src" / "app.py").write_text("def handler():\n    return 'zxq_marker'\n")
    (root / ".env").write_text("TOKEN=abc\n")
    (root / "leak.txt").write_text(f"{AWS}\n")
    git(root, "init", "-q")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "init")
    return root


@pytest.fixture
def runs(tmp_path: Path) -> Path:
    return tmp_path / "runs"


def run(coroutine: Any) -> Any:
    return asyncio.run(coroutine)


def session_of(project: Path, runs: Path, profile: str, **kw: Any) -> Any:
    server, session = mcp_server.build_server(project, runs, profile, **kw)
    session.start()
    return server, session


async def call_all(server: Any, calls: list[tuple[str, dict[str, Any]]]) -> list[Any]:
    async with Client(server) as client:
        return [await client.call_tool(name, args) for name, args in calls]


def text(result: Any) -> str:
    return "\n".join(c.text for c in result.content if hasattr(c, "text"))


def audit(runs: Path) -> list[dict[str, Any]]:
    chain = runs / ".mcp" / "provenance_mcp.jsonl"
    assert verify_provenance_chain(chain).ok
    return [json.loads(line) for line in chain.read_text().splitlines()]


# ── What a profile is offered ────────────────────────────────────────────────


def test_the_local_profile_is_offered_all_seven_tools_and_each_is_read_only(
    project: Path, runs: Path
) -> None:
    server, _ = session_of(project, runs, "local_project")

    async def listed() -> Any:
        async with Client(server) as client:
            return (await client.list_tools()).tools

    tools = run(listed())
    assert {t.name for t in tools} == mp.IMPLEMENTED_TOOLS
    for tool in tools:
        hints = tool.annotations
        assert hints.read_only_hint and not hints.destructive_hint and not hints.open_world_hint
        assert tool.description


def test_the_cloud_profile_is_offered_no_tool_that_returns_repository_content(
    project: Path, runs: Path
) -> None:
    server, _ = session_of(project, runs, "cloud_pipeline_info")

    async def listed() -> Any:
        async with Client(server) as client:
            return [t.name for t in (await client.list_tools()).tools]

    assert sorted(run(listed())) == ["models", "pipeline_guideline", "run_status"]


def test_a_tool_the_profile_does_not_offer_cannot_be_called_and_the_attempt_is_logged(
    project: Path, runs: Path
) -> None:
    server, _ = session_of(project, runs, "cloud_pipeline_info")
    results = run(
        call_all(
            server,
            [("repo_read", {"path": "src/app.py"}), ("approve_design", {}), ("decide", {"a": 1})],
        )
    )
    assert all(r.is_error and "Unknown tool" in text(r) for r in results)
    records = audit(runs)[1:]  # the first is the session start
    assert [(r["tool"], r["allowed"], r["reason_code"]) for r in records] == [
        ("repo_read", False, "tool_not_offered"),
        ("approve_design", False, "tool_not_offered"),
        ("decide", False, "tool_not_offered"),
    ]


def test_a_project_that_lets_its_source_tree_reach_the_cloud_gets_repository_tools_there(
    project: Path, runs: Path
) -> None:
    classes = dict(mp.load_data_classes(project))
    classes["artifact_defaults"] = {
        **classes["artifact_defaults"],
        "pinned_source_tree": "internal",
    }
    doc = (
        (project / "policy" / "tools.yaml")
        .read_text()
        .replace(
            "tools: [run_status, pipeline_guideline, models]",
            "tools: [repo_read, run_status, pipeline_guideline, models]",
        )
    )
    policy = mp.parse_tools_policy(doc.encode(), classes)
    server, _ = session_of(
        project, runs, "cloud_pipeline_info", policy=policy, data_classes=classes
    )
    (result,) = run(call_all(server, [("repo_read", {"path": "src/app.py"})]))
    assert not result.is_error and "zxq_marker" in text(result)


def test_a_profile_that_does_not_exist_is_refused_with_the_ones_that_do(
    project: Path, runs: Path
) -> None:
    with pytest.raises(mp.ToolsPolicyError, match="cloud_pipeline_info, local_project"):
        mcp_server.build_server(project, runs, "nope")


# ── Files ────────────────────────────────────────────────────────────────────


def test_files_are_read_listed_and_searched_through_the_policy(project: Path, runs: Path) -> None:
    server, _ = session_of(project, runs, "local_project")
    listing, content, found = run(
        call_all(
            server,
            [
                ("repo_list", {"path": "src"}),
                ("repo_read", {"path": "src/app.py", "length": 12}),
                ("repo_search", {"query": "zxq_marker"}),
            ],
        )
    )
    assert [e["name"] for e in json.loads(text(listing))["entries"]] == ["app.py"]
    body = json.loads(text(content))
    assert body["text"] == "def handler(" and body["truncated"]
    assert [m["path"] for m in json.loads(text(found))["matches"]] == ["src/app.py"]


@pytest.mark.parametrize(
    ("tool", "args", "code"),
    [
        ("repo_read", {"path": ".env"}, "unreadable_path"),
        ("repo_read", {"path": ".git/config"}, "unreadable_path"),
        ("repo_read", {"path": "../outside"}, "path_outside_root"),
        ("repo_read", {"path": "/etc/passwd"}, "path_outside_root"),
        ("repo_read", {"path": "leak.txt"}, "secret_in_content"),
        ("repo_read", {"path": "missing.txt"}, "not_found"),
        ("repo_list", {"path": ".runs"}, "unreadable_path"),
        ("repo_search", {"query": ""}, "bad_argument"),
    ],
)
def test_a_refused_file_call_says_why_and_is_logged_as_a_denial(
    project: Path, runs: Path, tool: str, args: dict[str, Any], code: str
) -> None:
    server, _ = session_of(project, runs, "local_project")
    (result,) = run(call_all(server, [(tool, args)]))
    assert result.is_error and f"denied ({code})" in text(result)
    assert AWS not in text(result) and "TOKEN=abc" not in text(result)
    last = audit(runs)[-1]
    assert (last["tool"], last["allowed"], last["reason_code"]) == (tool, False, code)


def test_a_session_stops_at_its_call_limit(project: Path, runs: Path) -> None:
    policy = mp.load_tools_policy(project)
    profile = policy.profiles["local_project"]
    tight = mp.Profile(profile.name, "", profile.destination, profile.tools, mp.Limits(1024, 2))
    server, _ = session_of(
        project,
        runs,
        "local_project",
        policy=mp.ToolsPolicy(policy.digest, {"local_project": tight}),
    )
    results = run(call_all(server, [("repo_list", {})] * 3))
    assert [r.is_error for r in results] == [False, False, True]
    assert "denied (call_limit)" in text(results[2])


def test_a_result_larger_than_the_profile_allows_is_refused(project: Path, runs: Path) -> None:
    (project / "big").mkdir()
    for i in range(80):
        (project / "big" / f"file_{i:03d}_{'x' * 40}.txt").write_text("x")
    policy = mp.load_tools_policy(project)
    profile = policy.profiles["local_project"]
    tight = mp.Profile(profile.name, "", profile.destination, profile.tools, mp.Limits(1024, 10))
    server, _ = session_of(
        project,
        runs,
        "local_project",
        policy=mp.ToolsPolicy(policy.digest, {"local_project": tight}),
    )
    (result,) = run(call_all(server, [("repo_list", {"path": "big"})]))
    assert result.is_error and "denied (too_large)" in text(result)


# ── Runs and the pipeline ────────────────────────────────────────────────────


def make_run(project: Path, runs: Path, run_id: str = "demo") -> Path:
    spec = project / "spec.json"
    spec.write_text(json.dumps(SPEC))
    run_dir = runs / run_id
    init_run(project, run_dir, run_id, "START", spec)
    return run_dir


def test_a_runs_status_is_read_without_changing_it(project: Path, runs: Path) -> None:
    run_dir = make_run(project, runs)
    before = (run_dir / "state.json").read_bytes()
    server, _ = session_of(project, runs, "cloud_pipeline_info")
    (result,) = run(call_all(server, [("run_status", {"run_id": "demo"})]))
    status = json.loads(text(result))
    assert status["run_id"] == "demo" and status["current"] == "START"
    assert status["runner_active"] is False
    assert (run_dir / "state.json").read_bytes() == before


def test_an_unknown_or_malformed_run_is_refused_without_the_hosts_paths(
    project: Path, runs: Path
) -> None:
    server, _ = session_of(project, runs, "local_project")
    missing, bad = run(
        call_all(server, [("run_status", {"run_id": "nope"}), ("run_status", {"run_id": "../x"})])
    )
    assert "denied (not_found): no run nope" in text(missing)
    assert "denied (bad_argument)" in text(bad)
    assert str(runs) not in text(missing) + text(bad)


def test_the_saved_evidence_report_is_returned_and_nothing_is_generated(
    project: Path, runs: Path
) -> None:
    run_dir = make_run(project, runs)
    server, _ = session_of(project, runs, "local_project")
    (absent,) = run(call_all(server, [("run_evidence", {"run_id": "demo"})]))
    assert "denied (not_found)" in text(absent) and "/evidence" in text(absent)
    assert not (run_dir / "approvals").exists()

    (run_dir / "approvals").mkdir()
    (run_dir / "approvals" / "START-evidence.md").write_text("# Evidence\n\nall good\n")
    (found,) = run(call_all(server, [("run_evidence", {"run_id": "demo"})]))
    body = json.loads(text(found))
    assert body["node"] == "START" and body["markdown"].endswith("all good\n")
    assert body["report"] == "approvals/START-evidence.md" and not body["truncated"]


def test_an_evidence_report_with_a_secret_is_withheld(project: Path, runs: Path) -> None:
    run_dir = make_run(project, runs)
    (run_dir / "approvals").mkdir()
    (run_dir / "approvals" / "START-evidence.md").write_text(f"key {AWS}\n")
    server, _ = session_of(project, runs, "local_project")
    (result,) = run(call_all(server, [("run_evidence", {"run_id": "demo"})]))
    assert "denied (secret_in_content)" in text(result) and AWS not in text(result)


def test_the_guideline_and_the_model_catalog_come_from_the_projects_own_code(
    project: Path, runs: Path, monkeypatch: Any
) -> None:
    seen: list[Path] = []

    def fake_guideline(root: Path, runs_dir: Path) -> dict[str, Any]:
        seen.append(root)
        return {"ready": True, "steps": [], "notes": [], "errors": []}

    monkeypatch.setattr(mcp_server.guideline, "build", fake_guideline)
    monkeypatch.setattr(mcp_server.registry_changes, "gaps", lambda root: [])
    monkeypatch.setattr(mcp_server, "catalog_rows", lambda root, deps: [{"name": "m1"}])
    server, _ = session_of(project, runs, "cloud_pipeline_info")
    guide, models = run(call_all(server, [("pipeline_guideline", {}), ("models", {})]))
    assert json.loads(text(guide))["ready"] is True and seen == [project]
    assert json.loads(text(models)) == {"models": [{"name": "m1"}], "gaps": [], "errors": []}


# ── The audit ────────────────────────────────────────────────────────────────


def test_the_log_records_each_call_with_the_profile_the_policy_digest_and_no_arguments(
    project: Path, runs: Path
) -> None:
    server, session = session_of(project, runs, "local_project")
    run(call_all(server, [("repo_read", {"path": "src/app.py"}), ("repo_read", {"path": ".env"})]))
    records = audit(runs)
    assert [(r["tool"], r["allowed"]) for r in records] == [
        ("(session)", True),
        ("repo_read", True),
        ("repo_read", False),
    ]
    policy = mp.load_tools_policy(project)
    assert {r["policy_digest"] for r in records} == {policy.digest}
    assert {r["profile"] for r in records} == {"local_project"}
    assert {r["session"] for r in records} == {session.id}
    assert all("src/app.py" not in json.dumps(r) for r in records)
    assert records[1]["args_hash"] == mp.arguments_hash(
        {"path": "src/app.py", "offset": 0, "length": None}
    )


def test_a_tool_cannot_return_when_its_call_cannot_be_logged(
    project: Path, runs: Path, monkeypatch: Any
) -> None:
    server, session = session_of(project, runs, "local_project")

    def broken(_: Any) -> Any:
        raise OSError("disk full")

    monkeypatch.setattr(session.log, "append", broken)
    (result,) = run(call_all(server, [("repo_read", {"path": "src/app.py"})]))
    assert result.is_error and "zxq_marker" not in text(result)
    assert "audit_unavailable" in text(result)


# ── The command line ─────────────────────────────────────────────────────────


def test_the_server_runs_over_stdio_as_a_subprocess(project: Path, runs: Path) -> None:
    params = StdioServerParameters(
        command=sys.executable,
        args=[
            "-m",
            "agentic_pipeline",
            "serve",
            "mcp",
            "--profile",
            "local_project",
            "--root",
            str(project),
            "--runs-dir",
            str(runs),
        ],
    )

    async def over_stdio() -> tuple[list[str], str, bool]:
        async with Client(params) as client:
            names = [t.name for t in (await client.list_tools()).tools]
            read = await client.call_tool("repo_read", {"path": "src/app.py"})
            refused = await client.call_tool("repo_read", {"path": ".env"})
            return names, text(read), refused.is_error

    names, content, refused = run(over_stdio())
    assert set(names) == mp.IMPLEMENTED_TOOLS and "zxq_marker" in content and refused
    assert [r["reason_code"] for r in audit(runs)][-2:] == ["ok", "unreadable_path"]


def test_the_command_reports_a_profile_that_does_not_exist(project: Path, runs: Path) -> None:
    from agentic_pipeline.cli import main

    code = main(
        ["serve", "mcp", "--profile", "nope", "--root", str(project), "--runs-dir", str(runs)]
    )
    assert code == 1
