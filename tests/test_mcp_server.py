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
    (root / "src" / "app.py").write_text("def handler():\n    return 'zxq_marker'\n", newline="\n")
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


def test_a_live_cloud_session_is_sent_only_the_files_the_path_rules_clear(
    project: Path, runs: Path
) -> None:
    classes = dict(mp.load_data_classes(project))
    classes["path_rules"] = [{"path": "schemas/**", "class": "internal"}]
    doc = (
        (project / "policy" / "tools.yaml")
        .read_text()
        .replace(
            "tools: [run_status, pipeline_guideline, models]",
            "tools: [repo_list, repo_read, repo_search, run_evidence, run_status]",
        )
    )
    policy = mp.parse_tools_policy(doc.encode(), classes)
    server, session = session_of(
        project, runs, "cloud_pipeline_info", policy=policy, data_classes=classes
    )
    assert "repo_read" in session.offered and "run_evidence" not in session.offered
    schema, source, listing, found = run(
        call_all(
            server,
            [
                ("repo_read", {"path": "schemas/proposal.schema.json", "length": 20}),
                ("repo_read", {"path": "src/app.py"}),
                ("repo_list", {"path": "."}),
                ("repo_search", {"query": "zxq_marker"}),
            ],
        )
    )
    assert not schema.is_error
    assert source.is_error and "denied (class_not_allowed)" in text(source)
    assert [e["name"] for e in json.loads(text(listing))["entries"]] == ["schemas"]
    assert json.loads(text(found))["matches"] == []
    refused = [r["reason_code"] for r in audit(runs) if r.get("allowed") is False]
    assert refused == ["class_not_allowed"]


def test_a_live_session_is_refused_a_policy_whose_rules_cannot_be_read(
    project: Path, runs: Path
) -> None:
    classes = dict(mp.load_data_classes(project))
    classes["path_rules"] = [{"path": "docs/", "class": "internal"}]
    with pytest.raises(mp.ToolsPolicyError, match="an empty segment"):
        mcp_server.build_server(project, runs, "local_project", data_classes=classes)


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
    (run_dir / "approvals" / "START-evidence.md").write_text(
        "# Evidence\n\nall good\n", newline="\n"
    )
    (found,) = run(call_all(server, [("run_evidence", {"run_id": "demo"})]))
    body = json.loads(text(found))
    assert body["node"] == "START" and body["markdown"].endswith("all good\n")
    assert body["report"] == "approvals/START-evidence.md" and not body["truncated"]


def test_an_evidence_report_with_a_secret_is_withheld(project: Path, runs: Path) -> None:
    run_dir = make_run(project, runs)
    (run_dir / "approvals").mkdir()
    (run_dir / "approvals" / "START-evidence.md").write_text(f"key {AWS}\n", newline="\n")
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


# ── Sessions bound to one operation of one run ───────────────────────────────

GROUNDED_RUN = "demo"


@pytest.fixture
def bound(tmp_path: Path) -> tuple[Path, Path]:
    return build_bound(tmp_path)


def build_bound(
    tmp_path: Path,
    path_rules: list[dict[str, str]] | None = None,
    cloud_reads: tuple[str, ...] = ("applicable_contracts",),
) -> tuple[Path, Path]:
    """A project whose registry gives two operations MCP tools, with a run that has finished
    grounding (so the pinned snapshot is sealed). `path_rules` go into its data classes and
    `cloud_reads` are the `tool_reads` of the cloud operation. Returns (project, runs directory)."""
    import yaml
    from test_grounding import Rig as GroundingRig

    from agentic_pipeline.grounding import GROUNDING_HANDLERS

    work = tmp_path / "work"
    work.mkdir()
    for name in ("pipeline", "policy", "schemas", "agents"):
        shutil.copytree(ROOT / name, work / name)
    registry_file = work / "policy" / "model_registry.yaml"
    registry = yaml.safe_load(registry_file.read_text())
    e1 = registry["stage_requirements"]["E1"]["operations"][0]
    e1.update(
        tool_profile="local_project",
        tool_reads=["pinned_source_tree", "applicable_contracts", "protected_tests"],
    )
    a1 = registry["stage_requirements"]["A1"]["operations"][0]
    a1.update(tool_profile="cloud_contracts", tool_reads=list(cloud_reads))
    registry_file.write_text(yaml.safe_dump(registry, sort_keys=False))
    if path_rules is not None:
        classes_file = work / "policy" / "data_classes.yaml"
        classes_doc = yaml.safe_load(classes_file.read_text())
        classes_doc["path_rules"] = path_rules
        classes_file.write_text(yaml.safe_dump(classes_doc, sort_keys=False))
    tools_file = work / "policy" / "tools.yaml"
    tools = yaml.safe_load(tools_file.read_text())
    tools["profiles"]["cloud_contracts"] = {
        "destination": "cloud_service",
        "tools": ["repo_list", "repo_read", "repo_search", "run_status", "run_evidence"],
        "limits": {"max_read_bytes": 65536, "max_calls": 50},
    }
    tools_file.write_text(yaml.safe_dump(tools, sort_keys=False))
    for rel, body in {
        "src/app.py": "def handler():\n    return 'zxq_marker'\n",
        "contracts/api.md": "# API contract zxq_marker\n",
        "tests/test_x.py": "def test_x(): ...\n",
        ".env": "TOKEN=abc\n",
        "leak.txt": f"{AWS}\n",
    }.items():
        path = work / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, newline="\n")
    git(work, "init", "-q")
    git(work, "add", "-A")
    git(work, "commit", "-q", "-m", "init")

    rig = GroundingRig(work, tmp_path)
    runner = rig.runner()
    runner.step()  # START
    GROUNDING_HANDLERS["G"](rig.ctx(runner, "G"))
    runner.close()
    return work, tmp_path / "runs"


def bound_server(bound: tuple[Path, Path], operation: str, **kw: Any) -> Any:
    project, runs = bound
    server, session = mcp_server.build_server(
        project, runs, run=GROUNDED_RUN, operation=operation, **kw
    )
    session.start()
    return server, session


def test_a_run_bound_session_has_the_profile_the_runs_registry_gives_the_operation(
    bound: tuple[Path, Path],
) -> None:
    server, session = bound_server(bound, "E1.implementation_patch")

    async def listed() -> Any:
        async with Client(server) as client:
            return {t.name for t in (await client.list_tools()).tools}

    assert run(listed()) == mp.IMPLEMENTED_TOOLS
    assert session.profile.name == "local_project"
    first = audit(bound[1])[0]
    assert first["bound_run"] == GROUNDED_RUN and first["operation"] == "E1.implementation_patch"
    assert first["artifacts"] == ["pinned_source_tree", "applicable_contracts", "protected_tests"]


def test_a_run_bound_session_reads_the_snapshot_and_not_the_live_checkout(
    bound: tuple[Path, Path],
) -> None:
    project, _ = bound
    (project / "src" / "app.py").write_text("def handler():\n    return 'edited later'\n")
    (project / "src" / "new.py").write_text("added = 'after grounding'\n")
    server, _ = bound_server(bound, "E1.implementation_patch")
    app, listing, found = run(
        call_all(
            server,
            [
                ("repo_read", {"path": "src/app.py"}),
                ("repo_list", {"path": "src"}),
                ("repo_search", {"query": "zxq_marker"}),
            ],
        )
    )
    assert "zxq_marker" in text(app) and "edited later" not in text(app)
    assert [e["name"] for e in json.loads(text(listing))["entries"]] == ["app.py"]
    paths = {m["path"] for m in json.loads(text(found))["matches"]}
    assert paths == {"src/app.py", "contracts/api.md"}


def test_the_snapshot_keeps_secrets_unreadable_too(bound: tuple[Path, Path]) -> None:
    server, _ = bound_server(bound, "E1.implementation_patch")
    env, leak = run(
        call_all(server, [("repo_read", {"path": ".env"}), ("repo_read", {"path": "leak.txt"})])
    )
    assert "denied (unreadable_path)" in text(env)
    assert "denied (secret_in_content)" in text(leak) and AWS not in text(leak)


def test_a_cloud_operation_reads_only_the_artifacts_whose_class_its_destination_may_receive(
    bound: tuple[Path, Path],
) -> None:
    server, session = bound_server(bound, "A1.proposal_a")
    assert session.profile.name == "cloud_contracts"
    # run_evidence is withheld: the destination may not receive repository content.
    assert "run_evidence" not in session.offered and "repo_read" in session.offered
    contract, source, listing, found = run(
        call_all(
            server,
            [
                ("repo_read", {"path": "contracts/api.md"}),
                ("repo_read", {"path": "src/app.py"}),
                ("repo_list", {"path": "."}),
                ("repo_search", {"query": "zxq_marker"}),
            ],
        )
    )
    assert "API contract" in text(contract)
    assert source.is_error and "denied (not_found)" in text(source)  # not even mounted
    assert [e["name"] for e in json.loads(text(listing))["entries"]] == ["contracts"]
    assert [m["path"] for m in json.loads(text(found))["matches"]] == ["contracts/api.md"]


CLEARED = [
    {"path": "schemas/**", "class": "internal"},
    {"path": "agents/**", "class": "internal"},
]


def test_a_cloud_operation_that_may_fetch_the_tree_is_sent_only_what_the_path_rules_clear(
    tmp_path: Path,
) -> None:
    bound = build_bound(tmp_path, CLEARED, ("pinned_source_tree", "applicable_contracts"))
    # The project later drops the rules: the run keeps the ones it was frozen with.
    classes_file = bound[0] / "policy" / "data_classes.yaml"
    classes_file.write_text(classes_file.read_text().replace("path_rules", "gone_rules"))
    server, session = bound_server(bound, "A1.proposal_a")
    assert "repo_read" in session.offered and "run_evidence" not in session.offered
    contract, schema, source, tests, listing, found = run(
        call_all(
            server,
            [
                ("repo_read", {"path": "contracts/api.md"}),
                ("repo_read", {"path": "schemas/proposal.schema.json", "length": 40}),
                ("repo_read", {"path": "src/app.py"}),
                ("repo_read", {"path": "tests/test_x.py"}),
                ("repo_list", {"path": "."}),
                ("repo_search", {"query": "zxq_marker"}),
            ],
        )
    )
    assert "API contract" in text(contract) and not schema.is_error
    for refused in (source, tests):
        assert refused.is_error and "denied (class_not_allowed)" in text(refused)
    assert [e["name"] for e in json.loads(text(listing))["entries"]] == [
        "agents",
        "contracts",
        "schemas",
    ]
    assert [m["path"] for m in json.loads(text(found))["matches"]] == ["contracts/api.md"]


def test_a_run_whose_policy_has_a_rule_the_code_cannot_read_is_refused(tmp_path: Path) -> None:
    bound = build_bound(tmp_path, [{"path": "../up/**", "class": "internal"}])
    with pytest.raises(mp.ToolsPolicyError, match=r"path_rules\[0\]"):
        bound_server(bound, "E1.implementation_patch")


def test_the_run_tools_answer_only_for_the_bound_run(bound: tuple[Path, Path]) -> None:
    server, _ = bound_server(bound, "E1.implementation_patch")
    own, other = run(
        call_all(
            server,
            [("run_status", {"run_id": GROUNDED_RUN}), ("run_status", {"run_id": "someone-else"})],
        )
    )
    assert json.loads(text(own))["run_id"] == GROUNDED_RUN
    assert "denied (other_run)" in text(other)


def test_the_policy_files_a_run_uses_are_frozen_with_it(bound: tuple[Path, Path]) -> None:
    from agentic_pipeline.run_state import RunStore

    _, runs = bound
    state = RunStore(runs / GROUNDED_RUN).load()
    for rel in ("policy/tools.yaml", "policy/data_classes.yaml", "policy/model_registry.yaml"):
        assert rel in state.policy and (runs / GROUNDED_RUN / "frozen" / rel).is_file()


def test_the_session_uses_the_frozen_policy_and_not_the_projects_current_one(
    bound: tuple[Path, Path],
) -> None:
    project, _ = bound
    # The project later drops the tool fields and the profile: the run keeps the old rules.
    shutil.copy(ROOT / "policy" / "model_registry.yaml", project / "policy" / "model_registry.yaml")
    (project / "policy" / "tools.yaml").write_text("version: 1\nprofiles: {}\n")
    server, session = bound_server(bound, "E1.implementation_patch")
    assert session.profile.name == "local_project"
    assert audit(bound[1])[0]["policy_digest"] == session.policy_digest


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda p, r: p.update(operation="E1.nothing"), "no operation"),
        (lambda p, r: p.update(operation="A1.proposal_b"), "no tool_profile"),
        (lambda p, r: p.update(profile_name="cloud_contracts"), "uses profile"),
        (lambda p, r: p.update(backend="gemini_4_argon_medium"), "runs outside the host"),
        (lambda p, r: p.update(backend="no_such_model"), "not a model"),
        (lambda p, r: p.update(run="nope"), "run nope"),
        (lambda p, r: p.update(run="../x"), "run id"),
    ],
)
def test_a_session_that_does_not_add_up_is_refused_before_it_starts(
    bound: tuple[Path, Path], change: Any, message: str
) -> None:
    project, runs = bound
    args: dict[str, Any] = {"run": GROUNDED_RUN, "operation": "E1.implementation_patch"}
    change(args, runs)
    with pytest.raises(mp.ToolsPolicyError, match=message):
        mcp_server.build_server(project, runs, args.pop("profile_name", None), **args)


def test_a_backend_inside_the_host_is_accepted_for_a_local_profile(
    bound: tuple[Path, Path],
) -> None:
    server, session = bound_server(bound, "E1.implementation_patch", backend="qwen3_5_9b")
    assert session.profile.name == "local_project"


def test_a_sealed_artifact_that_changed_stops_the_session(bound: tuple[Path, Path]) -> None:
    _, runs = bound
    sealed_file = next(
        (runs / GROUNDED_RUN / "grounding" / "G" / "pinned_source_tree").rglob("app.py")
    )
    sealed_file.write_text("tampered\n")
    with pytest.raises(mp.ToolsPolicyError, match="content changed"):
        bound_server(bound, "E1.implementation_patch")


def test_a_frozen_file_that_no_longer_matches_its_digest_stops_the_session(
    bound: tuple[Path, Path],
) -> None:
    _, runs = bound
    frozen = runs / GROUNDED_RUN / "frozen" / "policy" / "tools.yaml"
    frozen.write_text(frozen.read_text() + "\n# edited\n")
    with pytest.raises(mp.ToolsPolicyError, match="does not match the recorded digest"):
        bound_server(bound, "E1.implementation_patch")


def test_a_run_that_began_before_tools_yaml_was_frozen_is_refused_with_the_reason(
    bound: tuple[Path, Path],
) -> None:
    _, runs = bound
    (runs / GROUNDED_RUN / "frozen" / "policy" / "tools.yaml").unlink()
    with pytest.raises(mp.ToolsPolicyError, match="began before runs froze that file"):
        bound_server(bound, "E1.implementation_patch")


def test_the_command_binds_a_session_to_a_run_and_refuses_half_of_the_pair(
    bound: tuple[Path, Path],
) -> None:
    from agentic_pipeline.cli import main

    project, runs = bound
    base = ["serve", "mcp", "--root", str(project), "--runs-dir", str(runs)]
    assert main([*base, "--run", GROUNDED_RUN]) == 1
    assert main([*base, "--operation", "E1.implementation_patch"]) == 1
    assert main(base) == 1  # neither a profile nor a run
    assert main([*base, "--run", GROUNDED_RUN, "--operation", "A1.proposal_b"]) == 1
