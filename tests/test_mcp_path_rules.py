"""Path rules in policy/data_classes.yaml: which class a file of the project has, and what a
caller outside the host is then sent, listed and searched."""

from __future__ import annotations

import copy
import re
from pathlib import Path
from typing import Any

import pytest
import yaml
from test_mcp_policy import symlink

from agentic_pipeline import mcp_policy as mp
from agentic_pipeline.manifest import SealedInput
from agentic_pipeline.path_rules import (
    check_path_rules,
    first_class,
    parse_path_rules,
)
from agentic_pipeline.policies import check_data_classes

ROOT = Path(__file__).resolve().parents[1]
DATA_CLASSES = yaml.safe_load((ROOT / "policy" / "data_classes.yaml").read_text())
CLASSES = DATA_CLASSES["classes"]


def doc_with(*rules: tuple[str, str]) -> dict[str, Any]:
    doc = copy.deepcopy(DATA_CLASSES)
    doc["path_rules"] = [{"path": path, "class": cls} for path, cls in rules]
    return doc


def rules_of(*rules: tuple[str, str]) -> tuple[Any, ...]:
    return parse_path_rules(doc_with(*rules))


# ── The rules themselves ─────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("pattern", "path", "covered"),
    [
        ("docs/**", "docs", True),  # ** takes no segment too
        ("docs/**", "docs/a.md", True),
        ("docs/**", "docs/a/b/c.md", True),
        ("docs/**", "docsx/a.md", False),
        ("docs/**", "src/docs/a.md", False),  # anchored at the project root
        ("docs/*", "docs/a.md", True),
        ("docs/*", "docs/a/b.md", False),  # * stays inside one segment
        ("*.md", "README.md", True),
        ("*.md", "docs/README.md", False),
        ("**/*.md", "README.md", True),
        ("**/*.md", "docs/a/README.md", True),
        ("**/*.md", "docs/a.txt", False),
        ("a/**/b", "a/b", True),
        ("a/**/b", "a/x/y/b", True),
        ("a/**/b", "a/x/y", False),
        ("**", "anything/at/all", True),
        ("README.md", "README.md", True),
        ("README.md", "readme.md", False),  # case is significant
        ("file?.txt", "file1.txt", True),
        ("file?.txt", "file12.txt", False),
        ("[ab]*.py", "bad.py", True),
        ("[ab]*.py", "cad.py", False),
    ],
)
def test_a_pattern_covers_the_paths_its_segments_describe(
    pattern: str, path: str, covered: bool
) -> None:
    (rule,) = rules_of((pattern, "internal"))
    assert rule.matches(tuple(path.split("/"))) is covered


@pytest.mark.parametrize(
    ("pattern", "parts", "reaches"),
    [
        ("docs/**", (), True),
        ("docs/**", ("docs",), True),
        ("docs/**", ("docs", "a", "b"), True),
        ("docs/**", ("src",), False),
        ("src/docs/**", ("src",), True),
        ("src/docs/**", ("src", "app.py"), False),
        ("a/b/c.md", ("a", "b"), True),
        ("a/b/c.md", ("a", "x"), False),
        ("a/b/c.md", ("a", "b", "c.md", "d"), False),  # it ends at the file
        ("**/x.md", ("any", "where"), True),
        ("DOCS/**", ("docs",), True),  # ignoring case, so a prune never errs towards no
    ],
)
def test_a_pattern_reaches_a_directory_when_some_path_below_it_may_be_covered(
    pattern: str, parts: tuple[str, ...], reaches: bool
) -> None:
    (rule,) = rules_of((pattern, "internal"))
    assert rule.reaches(parts) is reaches


def test_the_first_matching_rule_decides_and_case_can_be_ignored() -> None:
    rules = rules_of(("docs/private/**", "confidential"), ("docs/**", "internal"))
    assert first_class(rules, ("docs", "private", "x.md")) == "confidential"
    assert first_class(rules, ("docs", "x.md")) == "internal"
    assert first_class(rules, ("src", "x.md")) is None
    assert first_class(rules, ("DOCS", "private", "x.md")) is None
    assert first_class(rules, ("DOCS", "private", "x.md"), fold=True) == "confidential"


GOOD = [("docs/**", "internal"), ("README.md", "public"), ("**/*.md", "internal")]


def test_a_policy_with_no_rules_or_good_ones_checks() -> None:
    assert check_path_rules(DATA_CLASSES) == []
    assert check_path_rules(doc_with(*GOOD)) == []
    assert check_data_classes(doc_with(*GOOD)) == []
    assert check_data_classes(DATA_CLASSES) == []
    assert parse_path_rules(DATA_CLASSES) == ()


@pytest.mark.parametrize(
    ("rules", "message"),
    [
        ("docs/**", "path_rules must be a list"),
        (["docs/**"], "must be a mapping with exactly path and class"),
        ([{"path": "docs/**"}], "exactly path and class"),
        ([{"path": "docs/**", "class": "internal", "note": "x"}], "exactly path and class"),
        ([{"path": "docs/**", "class": "secret"}], "not a class of this file"),
        ([{"path": "docs/**", "class": ["internal"]}], "not a class of this file"),
        ([{"path": "", "class": "internal"}], "must be a non-empty string"),
        ([{"path": 3, "class": "internal"}], "must be a non-empty string"),
        ([{"path": "/etc/**", "class": "internal"}], "relative to the project root"),
        ([{"path": "C:/x", "class": "internal"}], "relative to the project root"),
        ([{"path": "a\\b", "class": "internal"}], "forward slashes"),
        ([{"path": "../x", "class": "internal"}], "a . or .. segment"),
        ([{"path": "./x", "class": "internal"}], "a . or .. segment"),
        ([{"path": "docs/", "class": "internal"}], "an empty segment"),
        ([{"path": "a//b", "class": "internal"}], "an empty segment"),
        ([{"path": "a**b", "class": "internal"}], "** is a whole segment"),
        ([{"path": "/".join(["a"] * 40), "class": "internal"}], "more than 32 segments"),
        (
            [{"path": "docs/**", "class": "internal"}, {"path": "docs/**", "class": "public"}],
            "repeats an earlier rule",
        ),
    ],
)
def test_a_rule_that_cannot_be_read_is_reported(rules: Any, message: str) -> None:
    doc = copy.deepcopy(DATA_CLASSES)
    doc["path_rules"] = rules
    assert any(message in e for e in check_path_rules(doc)), check_path_rules(doc)
    assert any(message in e for e in check_data_classes(doc))
    with pytest.raises(ValueError, match=re.escape(message)):
        parse_path_rules(doc)
    with pytest.raises(mp.ToolsPolicyError, match=re.escape(message)):
        mp.rules_of(doc)


# ── A tree to serve ──────────────────────────────────────────────────────────

MARK = "zxq_marker"
FILES = {
    "README.md": "readme",
    "notes.txt": "notes",  # no rule covers it
    "docs/guide.md": "guide",
    "docs/deep/more.md": "more",
    "docs/private/plan.md": "private plan",
    "src/app.py": "src",
    "src/docs/notes.md": "src docs",
    "tests/test_a.py": "tests",
}
RULES = (
    ("docs/private/**", "confidential"),  # before the broader rule: it wins
    ("docs/**", "internal"),
    ("README.md", "internal"),
)


def write_tree(base: Path, files: dict[str, str] | None = None) -> Path:
    for rel, body in (files or FILES).items():
        path = base / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(f"{body} {MARK}\n", newline="\n")
    return base


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    return write_tree(tmp_path / "tree")


def live(
    tree: Path,
    destination: str = "cloud_service",
    rules: tuple[Any, ...] = (),
    cls: str = "confidential",
) -> mp.View:
    cloud = mp.destination_is_cloud(DATA_CLASSES, destination)
    return mp.View.live(tree, cls, cloud, CLASSES, rules)


def cloud(tree: Path, *rules: tuple[str, str], cls: str = "confidential") -> mp.View:
    return live(tree, rules=rules_of(*(rules or RULES)), cls=cls)


def names(view: mp.View, rel: str = ".") -> list[str]:
    return [e["name"] for e in mp.view_list(view, rel)["entries"]]


def code_of(call: Any, *args: Any) -> str:
    with pytest.raises(mp.Denied) as caught:
        call(*args)
    return caught.value.code


def read(view: mp.View, rel: str) -> str:
    return mp.view_read(view, rel, 0, None, 4096)["text"]


def search(view: mp.View, rel: str = ".", query: str = MARK) -> dict[str, Any]:
    return mp.view_search(view, query, rel, None, False, 50, 65536)


# ── What a caller outside the host is sent ───────────────────────────────────


def test_a_caller_outside_the_host_reads_a_file_only_if_its_rule_class_is_allowed(
    tree: Path,
) -> None:
    view = cloud(tree)
    assert read(view, "README.md").startswith("readme")
    assert read(view, "docs/guide.md").startswith("guide")
    assert read(view, "docs/deep/more.md").startswith("more")
    for refused in ("docs/private/plan.md", "src/app.py", "src/docs/notes.md", "tests/test_a.py"):
        assert code_of(mp.view_read, view, refused, 0, None, 4096) == "class_not_allowed"
    assert code_of(mp.view_read, view, "notes.txt", 0, None, 4096) == "class_not_allowed"


def test_a_caller_inside_the_host_is_not_held_by_the_rules(tree: Path) -> None:
    view = live(tree, "local", rules_of(*RULES))
    assert read(view, "src/app.py").startswith("src")
    assert read(view, "docs/private/plan.md").startswith("private plan")
    assert "src" in names(view)


def test_a_path_no_rule_covers_has_its_mounts_class(tree: Path) -> None:
    # confidential for the tree: refused; the same tree classed internal: allowed.
    assert code_of(mp.view_read, cloud(tree), "notes.txt", 0, None, 4096) == "class_not_allowed"
    assert read(cloud(tree, cls="internal"), "notes.txt").startswith("notes")
    # a rule can also take a class away from a path whose mount would have allowed it
    raised = cloud(tree, ("src/**", "confidential"), cls="internal")
    assert code_of(mp.view_read, raised, "src/app.py", 0, None, 4096) == "class_not_allowed"
    assert read(raised, "docs/guide.md").startswith("guide")


def test_a_refusal_says_nothing_of_whether_the_path_exists(tree: Path) -> None:
    view = cloud(tree)
    for missing in ("src/nope.py", "docs/private/nope.md", "nope.txt"):
        assert code_of(mp.view_read, view, missing, 0, None, 4096) == "class_not_allowed"
        assert code_of(mp.view_list, view, missing) == "class_not_allowed"
        assert code_of(search, view, missing) == "class_not_allowed"
    # where the caller may look, a missing path is just missing
    for call, args in ((mp.view_read, (0, None, 4096)), (mp.view_list, ())):
        assert code_of(call, view, "docs/nope.md", *args) == "not_found"
    assert code_of(search, view, "docs/nope.md") == "not_found"


def test_a_directory_is_listed_only_if_it_holds_a_file_the_caller_may_have(tree: Path) -> None:
    view = cloud(tree)
    assert names(view) == ["docs", "README.md"]  # no src, tests or notes.txt
    assert names(view, "docs") == ["deep", "guide.md"]  # private holds only refused files
    assert code_of(mp.view_list, view, "docs/private") == "class_not_allowed"
    assert code_of(mp.view_list, view, "src") == "class_not_allowed"
    assert [e["kind"] for e in mp.view_list(view, ".")["entries"]] == ["dir", "file"]
    assert names(live(tree, "local", rules_of(*RULES))) == [
        "docs",
        "src",
        "tests",
        "notes.txt",
        "README.md",
    ]


def test_a_rule_for_a_file_deep_in_a_confidential_directory_shows_the_way_to_it(
    tree: Path,
) -> None:
    view = cloud(tree, ("src/docs/**", "internal"))
    assert names(view) == ["src"]
    assert names(view, "src") == ["docs"]  # app.py is refused and not listed
    assert names(view, "src/docs") == ["notes.md"]
    assert read(view, "src/docs/notes.md").startswith("src docs")
    assert code_of(mp.view_read, view, "src/app.py", 0, None, 4096) == "class_not_allowed"


def test_a_search_scans_and_returns_only_files_the_caller_may_have(tree: Path) -> None:
    view = cloud(tree)
    found = search(view)
    assert {m["path"] for m in found["matches"]} == {
        "README.md",
        "docs/guide.md",
        "docs/deep/more.md",
    }
    assert found["files_scanned"] == 3  # refused files are not even opened
    assert search(view, "docs", "private")["matches"] == []
    assert search(view, "docs/guide.md")["files_scanned"] == 1
    assert code_of(search, view, "src") == "class_not_allowed"
    assert code_of(search, view, "docs/private") == "class_not_allowed"
    assert code_of(search, view, "src/app.py") == "class_not_allowed"
    # a search of a whole mount with nothing allowed in it is refused
    assert code_of(search, cloud(tree, ("nowhere/**", "internal"))) == "class_not_allowed"


@pytest.mark.parametrize("glob", ["*.toml", "src/*", "docs/private/*", "nope/**"])
def test_a_glob_that_filters_out_every_allowed_file_gives_no_matches_and_no_refusal(
    tree: Path, glob: str
) -> None:
    # The root holds files the caller may have, so a search of it is allowed whatever the glob.
    view = cloud(tree)
    found = mp.view_search(view, MARK, ".", glob, False, 50, 65536)
    assert found["matches"] == [] and not found["truncated"]
    # and the same search of a directory with nothing the caller may have is still refused
    assert code_of(mp.view_search, view, MARK, "src", glob, False, 50, 65536) == "class_not_allowed"


def test_a_glob_reaches_only_the_files_the_caller_may_have(tree: Path) -> None:
    view = cloud(tree)
    # the glob also covers docs/private/plan.md, which the caller may not have
    found = mp.view_search(view, MARK, ".", "docs/*.md", False, 50, 65536)
    assert {m["path"] for m in found["matches"]} == {"docs/guide.md", "docs/deep/more.md"}


def test_a_search_can_reach_a_deep_allowed_file_through_a_confidential_directory(
    tree: Path,
) -> None:
    view = cloud(tree, ("src/docs/**", "internal"))
    assert [m["path"] for m in search(view)["matches"]] == ["src/docs/notes.md"]
    assert [m["path"] for m in search(view, "src")["matches"]] == ["src/docs/notes.md"]


def test_the_repository_tools_are_offered_once_a_rule_gives_the_caller_something(
    tree: Path,
) -> None:
    every = tuple(sorted(mp.IMPLEMENTED_TOOLS))
    profile = mp.Profile("p", "", "cloud_service", every, mp.Limits(1024, 1))

    def offered(view: mp.View) -> set[str]:
        return set(mp.offered_tools(profile, DATA_CLASSES, view))

    assert offered(live(tree)) == mp.IMPLEMENTED_TOOLS - mp.CONTENT_TOOLS  # no rules
    assert not cloud(tree, ("docs/**", "confidential")).any_allowed()
    assert offered(cloud(tree, ("docs/**", "confidential"))).isdisjoint(mp.REPO_TOOLS)
    # with an allowed rule the repository tools return, but run_evidence (a saved report can
    # carry code) still needs the whole tree's class
    got = offered(cloud(tree))
    assert mp.REPO_TOOLS <= got and "run_evidence" not in got


def test_a_link_is_judged_by_the_file_it_leads_to_as_well_as_by_its_own_path(tree: Path) -> None:
    symlink(tree / "docs" / "link.md", tree / "src" / "app.py")  # allowed path, refused file
    # refused paths, allowed file; the second sits where a rule reaches, so only the file's own
    # class (not the directory's) stands in the way
    symlink(tree / "src" / "alias.md", tree / "docs" / "guide.md")
    symlink(tree / "docs" / "private" / "alias.md", tree / "docs" / "guide.md")
    view = cloud(tree)
    assert code_of(mp.view_read, view, "docs/link.md", 0, None, 4096) == "class_not_allowed"
    for alias in ("src/alias.md", "docs/private/alias.md"):
        assert code_of(mp.view_read, view, alias, 0, None, 4096) == "class_not_allowed"
        assert code_of(search, view, alias) == "class_not_allowed"
    assert names(view, "docs") == ["deep", "guide.md"]
    assert code_of(search, view, "docs/link.md") == "class_not_allowed"
    found = search(view)
    assert {m["path"] for m in found["matches"]} == {
        "README.md",
        "docs/guide.md",
        "docs/deep/more.md",
    }
    assert all("src" not in m["text"] for m in found["matches"])


def test_a_rule_read_ignoring_case_can_only_make_a_path_stricter(tmp_path: Path) -> None:
    # On a file system that ignores case, notes/secret.md is notes/SECRET.md; where it does not,
    # it is another file. Either way the stricter reading decides, so the deny rule holds.
    tree = write_tree(tmp_path / "t", {"notes/secret.md": "s", "notes/open.md": "o"})
    view = cloud(tree, ("notes/SECRET.md", "confidential"), ("notes/**", "internal"))
    assert code_of(mp.view_read, view, "notes/secret.md", 0, None, 4096) == "class_not_allowed"
    assert read(view, "notes/open.md").startswith("o")
    assert names(view, "notes") == ["open.md"]


# ── Sealed artifacts ─────────────────────────────────────────────────────────


@pytest.fixture
def mounts(tmp_path: Path) -> dict[str, Path]:
    return {
        "pinned_source_tree": write_tree(tmp_path / "pinned", FILES),
        "applicable_contracts": write_tree(
            tmp_path / "contracts", {"c.md": "contract", "secret/s.md": "secret contract"}
        ),
        "synthesis_sealed": write_tree(tmp_path / "synthesis", {"s.md": "synthesis"}),
    }


def snapshot(mounts: dict[str, Path], rules: tuple[Any, ...], *artifacts: str) -> mp.View:
    classes = {
        "pinned_source_tree": "confidential",
        "applicable_contracts": "internal",
        "synthesis_sealed": "internal",
    }
    found = {a: SealedInput(mounts[a], classes[a]) for a in artifacts}
    return mp.snapshot_view(found, True, CLASSES, rules)


def test_the_rules_apply_to_the_pinned_artifacts_by_their_place_in_the_repository(
    mounts: dict[str, Path],
) -> None:
    rules = rules_of(("contracts/secret/**", "confidential"), ("docs/**", "internal"))
    view = snapshot(mounts, rules, "pinned_source_tree", "applicable_contracts")
    assert read(view, "docs/guide.md").startswith("guide")  # a rule lowers the tree's class
    assert read(view, "contracts/c.md").startswith("contract")  # no rule: the contracts' class
    # a rule raises the class of part of an artifact that is allowed as a whole
    assert (
        code_of(mp.view_read, view, "contracts/secret/s.md", 0, None, 4096) == "class_not_allowed"
    )
    assert names(view, "contracts") == ["c.md"]
    assert code_of(mp.view_read, view, "src/app.py", 0, None, 4096) == "class_not_allowed"
    assert names(view) == ["contracts", "docs"]
    assert {m["path"] for m in search(view)["matches"]} == {
        "contracts/c.md",
        "docs/guide.md",
        "docs/deep/more.md",
        "docs/private/plan.md",
    }


def test_an_artifact_that_is_not_a_tree_of_the_repository_keeps_its_class(
    mounts: dict[str, Path],
) -> None:
    # a rule that would refuse everything cannot reach a derived artifact under its own id
    view = snapshot(
        mounts, rules_of(("**", "confidential")), "pinned_source_tree", "synthesis_sealed"
    )
    assert read(view, "synthesis_sealed/s.md").startswith("synthesis")
    assert names(view) == ["synthesis_sealed"]
    assert code_of(mp.view_read, view, "docs/guide.md", 0, None, 4096) == "class_not_allowed"


def test_without_rules_a_view_behaves_as_it_did(mounts: dict[str, Path]) -> None:
    view = snapshot(mounts, (), "pinned_source_tree", "applicable_contracts")
    assert names(view) == ["contracts"]
    assert code_of(mp.view_read, view, "docs/guide.md", 0, None, 4096) == "class_not_allowed"
    assert {m["path"] for m in search(view)["matches"]} == {
        "contracts/c.md",
        "contracts/secret/s.md",
    }


def test_a_listing_that_runs_out_of_its_budget_leaves_the_directory_out(tree: Path) -> None:
    view = cloud(tree)
    (mount,) = view.mounts
    assert view.dir_visible(mount, ("docs",), mp.Probe())
    assert not view.dir_visible(mount, ("docs",), mp.Probe(left=0))  # fail closed


def test_a_listing_that_ran_out_of_files_to_look_at_says_it_is_truncated(
    tree: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    view = cloud(tree)
    assert not mp.view_list(view, "docs")["truncated"]
    monkeypatch.setattr(mp, "MAX_SEARCH_FILES", 0)
    listing = mp.view_list(view, "docs")
    assert [e["name"] for e in listing["entries"]] == ["guide.md"]  # deep could not be judged
    assert listing["truncated"]


def test_a_path_rule_does_not_open_what_confinement_refuses(tree: Path) -> None:
    (tree / ".env").write_text("TOKEN=abc\n")
    (tree / "docs" / ".env").write_text("TOKEN=abc\n")
    view = cloud(tree, ("**", "internal"), cls="internal")
    assert code_of(mp.view_read, view, ".env", 0, None, 4096) == "unreadable_path"
    assert code_of(mp.view_read, view, "docs/.env", 0, None, 4096) == "unreadable_path"
    assert ".env" not in names(view) and ".env" not in names(view, "docs")
