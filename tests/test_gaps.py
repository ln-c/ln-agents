"""Agents that have no model: how code finds them and drafts one change that gives each a model.

A gap is a spec file no `agents` entry owns, or an entry whose spec names no catalog model
(registry rule R20). While one exists no run starts; code drafts the coordinator named in
`LN-AGENTS.md` for each, nothing is written until a human confirms it, and a model never becomes
a primary.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from agentic_pipeline import registry_changes as rc
from agentic_pipeline import registry_edit
from agentic_pipeline.briefs import BEGIN, END
from agentic_pipeline.graph import loads_yaml
from agentic_pipeline.registry import Gap, agent_key, spec_gaps
from agentic_pipeline.validation import validate_repo

ROOT = Path(__file__).resolve().parents[1]
COORDINATOR = "gemini_3_8_flash_high"
NEW_FILE = "pipeline-extra-helper-agent.md"
NEW_SPEC = f"agents/{NEW_FILE}"
LEARNING_MODELS = (
    "    analysis_fallback: qwen3_5_9b\n    subscription_analysis: claude_cli_default\n"
)
AUTHORITY_MODELS = "    model_assistance: [claude_cli_default, gemini_3_8_flash_high, qwen3_5_9b]\n"


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    for name in ("pipeline", "policy", "schemas", "agents"):
        shutil.copytree(ROOT / name, root / name)
    shutil.copy(ROOT / "LN-AGENTS.md", root / "LN-AGENTS.md")
    return root


@pytest.fixture
def runs(tmp_path: Path) -> Path:
    return tmp_path / "runs"


def registry_text(repo: Path) -> str:
    return (repo / rc.REGISTRY).read_text(encoding="utf-8")


def registry(repo: Path) -> dict:
    return loads_yaml(registry_text(repo).encode("utf-8"))


def add_spec(repo: Path, name: str = NEW_FILE, *, brief: bool = True) -> None:
    """A spec file that no entry owns (the learning spec's text, which has a brief)."""
    text = (ROOT / "agents" / "pipeline-learning-agent.md").read_text(encoding="utf-8")
    if not brief:
        text = text.replace(BEGIN, "").replace(END, "")
    (repo / "agents" / name).write_text(text, encoding="utf-8")


def drop(repo: Path, block: str) -> None:
    text = registry_text(repo)
    assert text.count(block) == 1
    (repo / rc.REGISTRY).write_text(text.replace(block, ""), encoding="utf-8")


def refused(call, *args, **kwargs) -> list[str]:  # noqa: ANN001, ANN002, ANN003
    with pytest.raises(rc.ChangeError) as caught:
        call(*args, **kwargs)
    return caught.value.errors


# ── Finding the gaps ─────────────────────────────────────────────────────────


def test_the_repository_has_no_gap() -> None:
    assert rc.gaps(ROOT) == []


def test_a_spec_no_entry_owns_and_an_entry_with_no_model_are_the_two_kinds(repo: Path) -> None:
    add_spec(repo)
    drop(repo, LEARNING_MODELS)
    found = rc.gaps(repo)
    assert found == [Gap("no_model", "learning", "agents/pipeline-learning-agent.md")] + [
        Gap("no_entry", "extra_helper", NEW_SPEC)
    ]


def test_validate_reports_exactly_the_gaps_in_the_words_it_always_did(repo: Path) -> None:
    add_spec(repo)
    drop(repo, LEARNING_MODELS)
    assert validate_repo(repo) == [
        "registry R20 agents.learning: its spec 'agents/pipeline-learning-agent.md' names no "
        "catalog model",
        f"registry R20: {NEW_SPEC} is not the spec of any agents entry",
    ]


def test_a_spec_that_two_entries_share_is_not_a_gap(repo: Path) -> None:
    reg = registry(repo)
    reg["agents"]["release"]["spec"] = reg["agents"]["learning"]["spec"]
    assert spec_gaps(reg, repo) == [Gap("no_entry", "release", "agents/pipeline-release-agent.md")]


def test_an_unreadable_registry_has_no_gaps_to_report(tmp_path: Path) -> None:
    assert rc.gaps(tmp_path) == []
    (tmp_path / "policy").mkdir()
    (tmp_path / rc.REGISTRY).write_text("agents: 3\n", encoding="utf-8")
    assert rc.gaps(tmp_path) == []


@pytest.mark.parametrize(
    ("file", "key"),
    [
        ("pipeline-foo-bar-agent.md", "foo_bar"),
        ("pipeline-exception-router.md", "exception_router"),
        ("pipeline-human-authority-agent.md", "human_authority"),
        ("my-helper.md", "my_helper"),
        ("9lives.md", "agent_9lives"),
        ("---.md", "agent"),
    ],
)
def test_a_new_entry_is_named_after_its_file_the_way_the_others_are(file: str, key: str) -> None:
    assert agent_key(file) == key


# ── The draft that closes them ───────────────────────────────────────────────


def test_a_spec_no_entry_owns_gets_an_inert_entry_with_the_coordinator(
    repo: Path, runs: Path
) -> None:
    add_spec(repo)
    before = registry_text(repo)
    draft = rc.propose_fill(repo, runs)
    assert draft.kind == "fill_gaps" and "no model chose this" in draft.summary
    assert f"+  extra_helper:\n+    spec: {NEW_SPEC}\n+    primary: {COORDINATOR}" in draft.diff
    assert any("no stage wiring" in n and "serves no operation" in n for n in draft.notes)
    assert any("leaves this host" in n for n in draft.notes), "the coordinator is a cloud model"
    assert registry_text(repo) == before, "a draft writes nothing"
    rc.apply(repo, runs, draft.change_id)
    assert registry(repo)["agents"]["extra_helper"] == {"spec": NEW_SPEC, "primary": COORDINATOR}
    assert validate_repo(repo) == [] and rc.gaps(repo) == []


def test_an_entry_with_no_model_gets_model_assistance_and_keeps_its_primary(
    repo: Path, runs: Path
) -> None:
    drop(repo, LEARNING_MODELS)
    primary = registry(repo)["agents"]["learning"]["primary"]
    draft = rc.propose_fill(repo, runs)
    assert f"+    model_assistance: [{COORDINATOR}]" in draft.diff
    assert "primary" not in draft.diff.replace("is untouched", "")
    rc.apply(repo, runs, draft.change_id)
    learning = registry(repo)["agents"]["learning"]
    assert learning["model_assistance"] == [COORDINATOR] and learning["primary"] == primary
    assert validate_repo(repo) == []


def test_a_model_never_becomes_the_human_authority(repo: Path, runs: Path) -> None:
    drop(repo, AUTHORITY_MODELS)
    before = registry(repo)["agents"]["human_authority"]
    assert [g.agent for g in rc.gaps(repo)] == ["human_authority"]
    rc.apply(repo, runs, rc.propose_fill(repo, runs).change_id)
    after = registry(repo)["agents"]["human_authority"]
    assert after["model_assistance"] == [COORDINATOR], "now the coordinator may assist"
    assert (
        after["primary"] == before["primary"] == "human_approval_ui_and_deterministic_state_machine"
    )
    assert after["approval_decider"] == "human_only"
    assert validate_repo(repo) == []


def test_an_existing_model_assistance_list_keeps_what_it_held(repo: Path, runs: Path) -> None:
    drop(repo, LEARNING_MODELS)
    text = registry_text(repo).replace(
        "    index_update_condition",
        "    model_assistance: [deterministic_rules]\n    index_update_condition",
        1,
    )
    (repo / rc.REGISTRY).write_text(text, encoding="utf-8")
    rc.apply(repo, runs, rc.propose_fill(repo, runs).change_id)
    assert registry(repo)["agents"]["learning"]["model_assistance"] == [
        "deterministic_rules",
        COORDINATOR,
    ]


def test_one_draft_closes_every_gap_and_comments_survive(repo: Path, runs: Path) -> None:
    add_spec(repo)
    drop(repo, LEARNING_MODELS)
    comments = [line for line in registry_text(repo).splitlines() if line.lstrip().startswith("#")]
    draft = rc.propose_fill(repo, runs)
    assert draft.summary.startswith("Give 2 agents a model")
    assert [p["id"] for p in rc.pending(runs)] == [draft.change_id]
    rc.apply(repo, runs, draft.change_id)
    assert rc.gaps(repo) == [] and validate_repo(repo) == []
    after = registry_text(repo).splitlines()
    assert all(c in after for c in comments), "no comment was lost"


def test_a_draft_that_closes_only_some_gaps_is_refused_whole(repo: Path, runs: Path) -> None:
    add_spec(repo)
    drop(repo, LEARNING_MODELS)
    before = registry_text(repo)
    errors = refused(rc.draft_fill_gaps, repo, runs, {"learning": COORDINATOR}, origin="test")
    assert errors == [
        "no model was chosen for extra_helper (agents/pipeline-extra-helper-agent.md)"
    ]
    assert rc.pending(runs) == [] and registry_text(repo) == before


def test_no_other_change_can_be_made_while_an_agent_has_no_model(repo: Path, runs: Path) -> None:
    add_spec(repo)
    errors = refused(
        rc.draft_bind, repo, runs, "design.proposal_b.primary", ["gpt_oss_120b_medium"]
    )
    assert errors[0].startswith("registry R20") and errors[-1] == rc.NO_MODEL_HINT
    assert refused(rc.draft_add_model, repo, runs, "google_ai_pro", "gemini-9-new", [])[-1] == (
        rc.NO_MODEL_HINT
    )
    assert rc.pending(runs) == []


def test_asking_twice_does_not_pile_up_drafts(repo: Path, runs: Path) -> None:
    add_spec(repo)
    first = rc.propose_fill(repo, runs)
    again = rc.propose_fill(repo, runs)
    assert again == first and [p["id"] for p in rc.pending(runs)] == [first.change_id]


def test_a_different_coordinator_is_a_different_draft(repo: Path, runs: Path) -> None:
    add_spec(repo)
    first = rc.propose_fill(repo, runs)
    (repo / "LN-AGENTS.md").write_text(
        (repo / "LN-AGENTS.md").read_text(encoding="utf-8").replace(COORDINATOR, "qwen3_5_9b"),
        encoding="utf-8",
    )
    second = rc.propose_fill(repo, runs)
    assert second.change_id != first.change_id and "primary: qwen3_5_9b" in second.diff
    assert not any("leaves this host" in n for n in second.notes), "a local model sends nothing out"
    assert len(rc.pending(runs)) == 2


def test_a_stale_draft_is_refused_when_the_registry_moved(repo: Path, runs: Path) -> None:
    add_spec(repo)
    draft = rc.propose_fill(repo, runs)
    (repo / rc.REGISTRY).write_text(registry_text(repo) + "# edited by hand\n", encoding="utf-8")
    assert "changed after change" in refused(rc.apply, repo, runs, draft.change_id)[0]


# ── What code will not guess ─────────────────────────────────────────────────


def test_without_a_coordinator_nothing_is_drafted_and_the_fix_is_named(
    repo: Path, runs: Path
) -> None:
    add_spec(repo)
    (repo / "LN-AGENTS.md").unlink()
    errors = refused(rc.propose_fill, repo, runs)
    assert "names no coordinator" in errors[0] and "`coordinator:`" in errors[0]
    assert rc.pending(runs) == []


def test_a_coordinator_that_is_not_in_the_catalog_is_not_swapped_for_another(
    repo: Path, runs: Path
) -> None:
    add_spec(repo)
    (repo / "LN-AGENTS.md").write_text(
        "---\ncoordinator: not_a_model\nfallback: qwen3_5_9b\n---\nbody\n", encoding="utf-8"
    )
    errors = refused(rc.propose_fill, repo, runs)
    assert "not_a_model" in errors[0] and "not in the catalog" in errors[0]


def test_a_new_entry_whose_name_is_taken_is_refused(repo: Path, runs: Path) -> None:
    add_spec(repo, "pipeline-human-authority.md")  # would be agents.human_authority
    assert "already exists with another spec" in refused(rc.propose_fill, repo, runs)[0]


def test_a_new_spec_without_a_brief_is_refused_with_the_brief_rule(repo: Path, runs: Path) -> None:
    add_spec(repo, brief=False)
    errors = refused(rc.propose_fill, repo, runs)
    assert any(e.startswith("registry R21 agents.extra_helper") for e in errors), errors
    assert rc.pending(runs) == []


def test_two_files_that_would_share_a_name_are_refused_naming_both(repo: Path, runs: Path) -> None:
    add_spec(repo, "pipeline-foo-agent.md")
    add_spec(repo, "foo.md")
    errors = refused(rc.propose_fill, repo, runs)
    assert errors == [
        "agents/foo.md and agents/pipeline-foo-agent.md would both be agents.foo; "
        "rename one of the files"
    ]
    assert rc.pending(runs) == []


def test_a_reason_for_each_agent_is_shown_with_the_draft_and_the_proposer_is_kept(
    repo: Path, runs: Path
) -> None:
    add_spec(repo)
    drop(repo, LEARNING_MODELS)
    meta = {"proposed_by": "m1", "authority_says": "Two picks."}
    choices = {"learning": COORDINATOR, "extra_helper": "qwen3_5_9b"}
    draft = rc.draft_fill_gaps(
        repo,
        runs,
        choices,
        origin="proposed by m1",
        reasons={"learning": "Suits analysis.", "extra_helper": "Local."},
        meta=meta,
    )
    assert "Why (proposed by m1): Suits analysis." in draft.notes
    assert "Why (proposed by m1): Local." in draft.notes
    assert draft.meta == meta
    waiting = rc.pending_fill(repo, runs)
    assert waiting is not None and waiting.change_id == draft.change_id and waiting.meta == meta


def test_a_draft_made_by_code_keeps_why_the_authority_was_not_used(repo: Path, runs: Path) -> None:
    add_spec(repo)
    by_code = rc.propose_fill(repo, runs, unavailable="no model is ready")
    assert by_code.meta == {"proposed_by": "code", "authority_unavailable": "no model is ready"}
    assert rc.propose_fill(repo, runs).meta == by_code.meta, "asking again returns the same draft"
    other = rc.propose_fill(repo, runs / "elsewhere")
    assert other.meta == {"proposed_by": "code"}, "without a reason, none is invented"


def test_a_waiting_fill_is_found_only_while_it_still_fits_the_registry(
    repo: Path, runs: Path
) -> None:
    assert rc.pending_fill(repo, runs) is None
    add_spec(repo)
    draft = rc.propose_fill(repo, runs)
    assert rc.pending_fill(repo, runs).change_id == draft.change_id  # type: ignore[union-attr]
    add_spec(repo, "pipeline-second-helper-agent.md")  # another gap: the draft no longer closes all
    assert rc.pending_fill(repo, runs) is None
    (repo / "agents" / "pipeline-second-helper-agent.md").unlink()
    assert rc.pending_fill(repo, runs) is not None
    (repo / rc.REGISTRY).write_text(registry_text(repo) + "\n# moved\n", encoding="utf-8")
    assert rc.pending_fill(repo, runs) is None, "the registry changed since it was drafted"


def test_the_newest_waiting_fill_wins(repo: Path, runs: Path) -> None:
    add_spec(repo)
    first = rc.draft_fill_gaps(repo, runs, {"extra_helper": COORDINATOR}, origin="one")
    second = rc.draft_fill_gaps(repo, runs, {"extra_helper": "qwen3_5_9b"}, origin="two")
    assert first.change_id != second.change_id
    found = rc.pending_fill(repo, runs)
    assert found is not None and found.change_id == second.change_id
    rc.discard(runs, second.change_id)
    found = rc.pending_fill(repo, runs)
    assert found is not None and found.change_id == first.change_id


def test_every_agent_already_having_a_model_is_not_a_draft(repo: Path, runs: Path) -> None:
    assert refused(rc.propose_fill, repo, runs) == ["every agent already has a model"]
    assert refused(rc.draft_fill_gaps, repo, runs, {}, origin="x") == [
        "every agent already has a model"
    ]


def test_the_refusal_to_start_names_every_agent_and_the_way_on() -> None:
    one = rc.refuse_start([Gap("no_entry", "x", "agents/x.md")])
    many = rc.refuse_start(
        [Gap("no_entry", "x", "agents/x.md"), Gap("no_model", "y", "agents/y.md")]
    )
    assert str(one).startswith("1 agent has no model (agents/x.md)")
    assert str(many).startswith("2 agents have no model (agents/x.md, agents/y.md)")
    assert "complete_mapping" in str(many) and isinstance(many, rc.MappingIncomplete)


# ── The text edits ───────────────────────────────────────────────────────────

TEXT = """\
# header comment
agents:
  one:
    spec: agents/one.md   # why
    primary: a

  two:
    spec: agents/two.md
    primary: b
    # trailing comment of two

# the next section
execution:
  x: 1
"""


def test_an_added_agent_goes_after_the_last_entry_and_before_the_next_section() -> None:
    new = registry_edit.add_agent(TEXT, "three", "agents/three.md", "m")
    assert new.endswith("# the next section\nexecution:\n  x: 1\n")
    assert "    primary: b\n\n  three:\n    spec: agents/three.md\n    primary: m\n" in new
    assert "    # trailing comment of two" in new and "# why" in new
    assert loads_yaml(new.encode())["agents"]["three"] == {
        "spec": "agents/three.md",
        "primary": "m",
    }


def test_an_added_key_is_indented_like_the_others_and_flow_lists_are_written_flow() -> None:
    new = registry_edit.add_key(TEXT, ["agents", "one"], "model_assistance", ["m", "n"])
    assert "    primary: a\n    model_assistance: [m, n]\n\n  two:" in new
    assert loads_yaml(new.encode())["agents"]["one"]["model_assistance"] == ["m", "n"]


@pytest.mark.parametrize(
    ("call", "why"),
    [
        (lambda: registry_edit.add_agent(TEXT, "one", "agents/x.md", "m"), "already has 'one'"),
        (lambda: registry_edit.add_agent(TEXT, "not usable", "agents/x.md", "m"), "usable agent"),
        (lambda: registry_edit.add_key(TEXT, ["agents", "one"], "primary", "m"), "already has"),
        (lambda: registry_edit.add_key(TEXT, ["agents", "nope"], "k", "m"), "no key 'nope'"),
        (lambda: registry_edit.add_key(TEXT, ["agents", "one", "spec"], "k", "m"), "block mapping"),
        (
            lambda: registry_edit.add_key(
                "agents:\n  one: {spec: s}\n", ["agents", "one"], "k", "m"
            ),
            "block mapping",
        ),
        (lambda: registry_edit.add_agent("agents: {}\n", "x", "agents/x.md", "m"), "block mapping"),
    ],
)
def test_an_edit_the_editor_cannot_make_as_text_is_refused(call, why: str) -> None:  # noqa: ANN001
    with pytest.raises(registry_edit.EditError, match=why):
        call()


def test_a_spec_path_that_is_not_plain_yaml_is_quoted() -> None:
    new = registry_edit.add_agent(TEXT, "odd", "agents/my agent: v2.md", "m")
    assert loads_yaml(new.encode())["agents"]["odd"]["spec"] == "agents/my agent: v2.md"
