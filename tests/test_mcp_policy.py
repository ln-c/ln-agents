"""The rules behind the local project MCP server: the policy file, the file confinement, the
secret scan and the audit log (no MCP SDK involved)."""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path
from typing import Any

import pytest
import yaml

from agentic_pipeline import mcp_policy as mp
from agentic_pipeline.provenance import verify_provenance_chain
from agentic_pipeline.validation import validate_repo

ROOT = Path(__file__).resolve().parents[1]
DATA_CLASSES = yaml.safe_load((ROOT / "policy" / "data_classes.yaml").read_text())
TOOLS = yaml.safe_load((ROOT / "policy" / "tools.yaml").read_text())
SCHEMA = json.loads((ROOT / "schemas" / "tools_policy.schema.json").read_text())

AWS = "AKIA" + "ABCDEFGHIJKLMNOP"  # a pattern match, not a credential


def symlink(link: Path, target: Path) -> None:
    try:
        link.symlink_to(target, target_is_directory=target.is_dir())
    except OSError:
        pytest.skip("symlinks are not available here")


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    files = {
        "README.md": "hello\nthe needle is here\n",
        "src/app.py": "def f():\n    return 1  # needle\n",
        "src/lib/util.py": "NEEDLE = 2\n",
        "docs/guide.md": "a guide\n",
        ".env": "TOKEN=abc\n",
        ".git/config": "[core]\n",
        ".runs/r1/state.json": "{}\n",
        "id_rsa": "private\n",
        "keys/server.pem": "pem\n",
        "keys/readme.txt": "needle in keys\n",
        "app-secrets.yaml": "needle: 1\n",
        "leak.txt": f"needle {AWS}\n",
        "ok.txt": "needle and nothing else\n",
        "node_modules/pkg/index.js": "needle\n",
    }
    for rel, text in files.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    (root / "bin").mkdir()
    (root / "bin" / "blob.bin").write_bytes(b"needle\x00\x01\x02")
    return root


@pytest.fixture
def outside(tmp_path: Path) -> Path:
    path = tmp_path / "outside"
    path.mkdir()
    (path / "secret.txt").write_text("needle outside\n")
    return path


# ── The policy file ──────────────────────────────────────────────────────────


def test_the_real_policy_loads_and_its_profiles_are_what_the_file_says() -> None:
    policy = mp.load_tools_policy(ROOT)
    assert policy.digest.startswith("sha256:") and len(policy.digest) == 71
    assert sorted(policy.profiles) == ["cloud_pipeline_info", "local_project"]
    local = policy.profiles["local_project"]
    assert local.destination == "local" and set(local.tools) == mp.IMPLEMENTED_TOOLS
    assert local.limits == mp.Limits(65536, 200)


def test_the_tools_the_code_implements_are_exactly_the_tools_the_schema_lists() -> None:
    assert set(SCHEMA["definitions"]["tool"]["enum"]) == mp.IMPLEMENTED_TOOLS


def test_no_tool_in_the_closed_list_can_approve_sign_decide_or_change_policy() -> None:
    forbidden = (
        "approv",
        "sign",
        "decide",
        "resume",
        "policy",
        "budget",
        "veto",
        "advance",
        "start",
    )
    assert not [t for t in mp.IMPLEMENTED_TOOLS if any(w in t for w in forbidden)]


def test_a_destination_outside_the_host_never_gets_repository_content_by_default() -> None:
    assert mp.content_allowed(DATA_CLASSES, "local")
    for kind in ("internal_service",):
        assert mp.content_allowed(DATA_CLASSES, kind)
    for kind in ("cloud_service", "third_party"):
        assert not mp.content_allowed(DATA_CLASSES, kind)


def test_a_destination_the_file_does_not_list_is_treated_as_outside_the_host() -> None:
    assert mp.destination_is_cloud(DATA_CLASSES, "a_new_kind")
    assert not mp.content_allowed(DATA_CLASSES, "a_new_kind")


def test_content_follows_the_class_the_project_gives_its_source_tree() -> None:
    classes = copy.deepcopy(DATA_CLASSES)
    classes["artifact_defaults"]["pinned_source_tree"] = "internal"
    assert mp.content_allowed(classes, "cloud_service")
    classes["artifact_defaults"]["pinned_source_tree"] = "no_such_class"
    assert not mp.content_allowed(classes, "cloud_service")
    del classes["artifact_defaults"]["pinned_source_tree"]
    assert not mp.content_allowed(classes, "cloud_service")


def test_a_profile_gets_only_the_tools_its_destination_may_use() -> None:
    cloud = mp.Profile(
        "p", "", "cloud_service", tuple(sorted(mp.IMPLEMENTED_TOOLS)), mp.Limits(1024, 1)
    )
    local = mp.Profile("p", "", "local", tuple(sorted(mp.IMPLEMENTED_TOOLS)), mp.Limits(1024, 1))
    assert set(mp.offered_tools(local, DATA_CLASSES)) == mp.IMPLEMENTED_TOOLS
    assert set(mp.offered_tools(cloud, DATA_CLASSES)) == mp.IMPLEMENTED_TOOLS - mp.CONTENT_TOOLS


def mutated(change: Any) -> dict[str, Any]:
    doc = copy.deepcopy(TOOLS)
    change(doc)
    return doc


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda d: d["profiles"]["local_project"]["tools"].append("approve"), "tools"),
        (lambda d: d["profiles"]["local_project"].update(destination="mars"), "destination"),
        (lambda d: d["profiles"]["cloud_pipeline_info"]["tools"].append("repo_read"), "repo_read"),
        (
            lambda d: d["profiles"]["cloud_pipeline_info"]["tools"].append("run_evidence"),
            "run_evidence",
        ),
        (lambda d: d.update(version=2), "tools"),
        (lambda d: d["profiles"].update({"Bad Name": d["profiles"]["local_project"]}), "tools"),
    ],
)
def test_a_policy_that_cannot_be_used_is_refused_with_the_reason(change: Any, message: str) -> None:
    errors = mp.check_tools_policy(mutated(change), DATA_CLASSES)
    assert errors and any(message in e for e in errors), errors


def test_a_cloud_profile_listing_a_content_tool_is_fine_once_the_class_allows_it() -> None:
    doc = mutated(lambda d: d["profiles"]["cloud_pipeline_info"]["tools"].append("repo_read"))
    classes = copy.deepcopy(DATA_CLASSES)
    classes["artifact_defaults"]["pinned_source_tree"] = "internal"
    assert mp.check_tools_policy(doc, classes) == []


def test_parse_reports_unreadable_yaml_and_errors_as_one_exception() -> None:
    with pytest.raises(mp.ToolsPolicyError, match="not readable YAML"):
        mp.parse_tools_policy(b"a: [unclosed", DATA_CLASSES)
    with pytest.raises(mp.ToolsPolicyError, match="destination"):
        mp.parse_tools_policy(
            yaml.safe_dump(
                mutated(lambda d: d["profiles"]["local_project"].update(destination="x"))
            ).encode(),
            DATA_CLASSES,
        )


def test_the_repository_validates_with_its_tools_policy_and_a_bad_one_is_reported(
    tmp_path: Path,
) -> None:
    assert validate_repo(ROOT) == []
    import shutil

    bad = tmp_path / "bad"
    for name in ("pipeline", "policy", "schemas", "agents"):
        shutil.copytree(ROOT / name, bad / name)
    doc = mutated(lambda d: d["profiles"]["cloud_pipeline_info"]["tools"].append("repo_read"))
    (bad / "policy" / "tools.yaml").write_text(yaml.safe_dump(doc))
    assert any("repo_read" in e for e in validate_repo(bad))
    (bad / "policy" / "tools.yaml").unlink()
    assert any("policy/tools.yaml" in e for e in validate_repo(bad))


# ── Paths ────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "rel",
    [
        "/etc/passwd",
        "C:/Windows",
        "c:x",
        "a\\b",
        "..",
        "../x",
        "a/../../x",
        "a\x00b",
        ".git/config",
        "src/.git/config",
        ".runs/r1/state.json",
        ".env",
        ".env.local",
        "id_rsa",
        "id_ed25519.pub",
        "keys/server.pem",
        "app-secrets.yaml",
        "node_modules/pkg",
        "x/.ssh/id",
    ],
)
def test_a_path_that_leaves_the_project_or_names_a_secret_is_refused(rel: str) -> None:
    with pytest.raises(mp.Denied) as caught:
        mp.relative_parts(rel)
    assert caught.value.code in {"bad_path", "path_outside_root", "unreadable_path"}


@pytest.mark.parametrize(
    ("rel", "parts"),
    [("", ()), (".", ()), ("src/app.py", ("src", "app.py")), ("./src//app.py", ("src", "app.py"))],
)
def test_a_path_inside_the_project_is_split_into_parts(rel: str, parts: tuple[str, ...]) -> None:
    assert mp.relative_parts(rel) == parts


def test_a_symlink_out_of_the_project_is_refused_and_one_inside_is_followed(
    project: Path, outside: Path
) -> None:
    symlink(project / "link_out", outside)
    symlink(project / "link_file", outside / "secret.txt")
    symlink(project / "link_in", project / "src")
    symlink(project / "looks_fine.txt", project / ".env")
    for rel in ("link_out", "link_out/secret.txt", "link_file"):
        with pytest.raises(mp.Denied) as caught:
            mp.resolve_readable(project, rel)
        assert caught.value.code == "path_outside_root"
    with pytest.raises(mp.Denied) as caught:
        mp.resolve_readable(project, "looks_fine.txt")  # a link to a secret is still a secret
    assert caught.value.code == "unreadable_path"
    assert mp.resolve_readable(project, "link_in/app.py").read_text().startswith("def f")


def test_a_missing_path_is_not_found(project: Path) -> None:
    with pytest.raises(mp.Denied) as caught:
        mp.resolve_readable(project, "nothing/here.txt")
    assert caught.value.code == "not_found"


# ── Listing, reading, searching ──────────────────────────────────────────────


def test_a_listing_shows_only_what_may_be_read(project: Path, outside: Path) -> None:
    symlink(project / "link_out", outside)
    symlink(project / "link_in", project / "src")
    listing = mp.list_dir(project, ".")
    names = [e["name"] for e in listing["entries"]]
    assert names == sorted(names, key=lambda n: (not (project / n).is_dir(), n.lower()))
    assert {"README.md", "docs", "src", "bin", "keys", "ok.txt", "link_in"} <= set(names)
    for hidden in (
        ".env",
        ".git",
        ".runs",
        "id_rsa",
        "app-secrets.yaml",
        "node_modules",
        "link_out",
    ):
        assert hidden not in names
    assert [e["name"] for e in mp.list_dir(project, "keys")["entries"]] == ["readme.txt"]
    kinds = {e["name"]: e["kind"] for e in listing["entries"]}
    assert kinds["src"] == "dir" and kinds["README.md"] == "file"


def test_a_listing_of_a_file_or_a_secret_directory_is_refused(project: Path) -> None:
    with pytest.raises(mp.Denied, match="not a directory"):
        mp.list_dir(project, "README.md")
    with pytest.raises(mp.Denied):
        mp.list_dir(project, ".git")


def test_a_read_returns_a_slice_and_says_when_there_is_more(project: Path) -> None:
    first = mp.read_text(project, "README.md", 0, 5, 1000)
    assert first["text"] == "hello" and first["truncated"] and first["size"] == 25
    rest = mp.read_text(project, "README.md", 5, None, 1000)
    assert rest["text"] == "\nthe needle is here\n" and not rest["truncated"]
    capped = mp.read_text(project, "README.md", 0, 1000, 8)
    assert capped["length"] == 8 and capped["truncated"]
    assert mp.read_text(project, "README.md", 1000, None, 100)["text"] == ""


@pytest.mark.parametrize(
    ("rel", "offset", "length", "code"),
    [
        ("bin/blob.bin", 0, None, "not_text"),
        ("leak.txt", 0, None, "secret_in_content"),
        (".env", 0, None, "unreadable_path"),
        ("src", 0, None, "not_a_file"),
        ("README.md", -1, None, "bad_argument"),
        ("README.md", 0, 0, "bad_argument"),
    ],
)
def test_a_read_that_the_rules_refuse_says_why(
    project: Path, rel: str, offset: int, length: int | None, code: str
) -> None:
    with pytest.raises(mp.Denied) as caught:
        mp.read_text(project, rel, offset, length, 1000)
    assert caught.value.code == code
    assert AWS not in str(caught.value)


def search(project: Path, query: str = "needle", **kw: Any) -> dict[str, Any]:
    args: dict[str, Any] = {
        "rel": ".",
        "glob": None,
        "ignore_case": False,
        "max_results": 50,
        "limit": 100000,
    }
    return mp.search_text(project, query, **{**args, **kw})


def test_a_search_finds_lines_only_in_readable_text_files(project: Path) -> None:
    found = search(project)
    paths = {m["path"] for m in found["matches"]}
    assert paths == {"README.md", "src/app.py", "ok.txt", "keys/readme.txt"}
    assert found["withheld_matches"] == 1  # leak.txt: the line matches a secret pattern
    assert not found["truncated"] and found["files_scanned"] >= 4
    line = next(m for m in found["matches"] if m["path"] == "src/app.py")
    assert line["line"] == 2 and line["text"] == "return 1  # needle"


def test_a_search_can_ignore_case_use_a_glob_and_start_below_the_root(project: Path) -> None:
    assert {m["path"] for m in search(project, ignore_case=True)["matches"]} >= {"src/lib/util.py"}
    assert {m["path"] for m in search(project, glob="src/*.py")["matches"]} == {"src/app.py"}
    assert {m["path"] for m in search(project, rel="keys")["matches"]} == {"keys/readme.txt"}
    assert [m["path"] for m in search(project, rel="README.md")["matches"]] == ["README.md"]


def test_a_search_stops_at_its_limits_and_says_so(project: Path) -> None:
    capped = search(project, max_results=1)
    assert len(capped["matches"]) == 1 and capped["truncated"]
    assert capped["stopped_by"] == "result limit"
    small = search(project, limit=60)
    assert small["truncated"] and len(small["matches"]) < 4


@pytest.mark.parametrize("query", ["", "x" * 201])
def test_a_search_needs_a_reasonable_query(project: Path, query: str) -> None:
    with pytest.raises(mp.Denied) as caught:
        search(project, query)
    assert caught.value.code == "bad_argument"


def test_a_search_does_not_follow_links_out_of_the_project(project: Path, outside: Path) -> None:
    symlink(project / "link_out", outside)
    assert "outside" not in json.dumps(search(project)["matches"])


@pytest.mark.parametrize("run_id", ["", "a/b", "..", "a b", "x" * 65, "ü"])
def test_a_run_id_names_one_directory(run_id: str) -> None:
    with pytest.raises(mp.Denied):
        mp.run_id(run_id)
    assert mp.run_id("deploy-design_1") == "deploy-design_1"


def test_the_secret_scan_reports_kinds_and_never_the_secret() -> None:
    assert mp.scan_secrets("nothing to see") == []
    assert mp.scan_secrets(f"key {AWS}") == ["aws_access_key"]
    assert mp.scan_secrets("-----BEGIN RSA PRIVATE KEY-----") == ["private_key"]


# ── The session and the audit log ────────────────────────────────────────────


def session(tmp_path: Path, calls: int = 3) -> mp.Session:
    profile = mp.Profile("p", "", "local", ("repo_read",), mp.Limits(1024, calls))
    return mp.Session(profile, "sha256:abc", tmp_path / "runs", ("repo_read",), "s1")


def test_a_session_refuses_calls_past_the_profiles_limit(tmp_path: Path) -> None:
    current = session(tmp_path, calls=2)
    current.begin()
    current.begin()
    with pytest.raises(mp.Denied) as caught:
        current.begin()
    assert caught.value.code == "call_limit"


def test_every_decision_is_logged_in_a_hash_chain_without_its_arguments(tmp_path: Path) -> None:
    current = session(tmp_path)
    current.start()
    current.record("repo_read", {"path": "README.md", "note": AWS}, True, "ok")
    current.record("repo_read", {"path": ".env"}, False, "unreadable_path")
    chain = tmp_path / "runs" / ".mcp" / "provenance_mcp.jsonl"
    records = [json.loads(line) for line in chain.read_text().splitlines()]
    assert verify_provenance_chain(chain).ok
    assert [(r["tool"], r["allowed"], r["reason_code"]) for r in records] == [
        ("(session)", True, "session_started"),
        ("repo_read", True, "ok"),
        ("repo_read", False, "unreadable_path"),
    ]
    assert {r["profile"] for r in records} == {"p"} and {r["session"] for r in records} == {"s1"}
    assert {r["policy_digest"] for r in records} == {"sha256:abc"}
    assert all(r["state"] == "MCP" and r["event"] == "decision" for r in records)
    assert records[1]["args_hash"] == mp.arguments_hash({"note": AWS, "path": "README.md"})
    assert AWS not in chain.read_text()  # only a hash of the arguments is kept


def test_a_call_that_cannot_be_logged_is_denied(tmp_path: Path, monkeypatch: Any) -> None:
    current = session(tmp_path)

    def broken(_: Any) -> Any:
        raise OSError("disk full")

    monkeypatch.setattr(current.log, "append", broken)
    with pytest.raises(mp.Denied) as caught:
        current.record("repo_read", {}, True, "ok")
    assert caught.value.code == "audit_unavailable"


def test_two_sessions_share_one_chain(tmp_path: Path) -> None:
    first, second = session(tmp_path), session(tmp_path)
    first.start()
    second.start()
    first.record("repo_read", {}, True, "ok")
    chain = tmp_path / "runs" / ".mcp" / "provenance_mcp.jsonl"
    assert verify_provenance_chain(chain).ok and len(chain.read_text().splitlines()) == 3


def test_the_unreadable_names_are_not_a_path_to_a_real_file_in_this_repository() -> None:
    # The defaults must not hide the project's own sources.
    for rel in ("src/agentic_pipeline/mcp_policy.py", "policy/tools.yaml", "README.md"):
        assert os.fspath(mp.resolve_readable(ROOT, rel)).endswith(Path(rel).name)
