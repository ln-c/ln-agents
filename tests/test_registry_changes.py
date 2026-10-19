"""Drafting, confirming and refusing changes to the model registry (on a copy of the repo)."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from agentic_pipeline import registry_changes as rc
from agentic_pipeline.graph import loads_yaml
from agentic_pipeline.validation import validate_repo

ROOT = Path(__file__).resolve().parents[1]


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


def refused(call, *args) -> list[str]:  # noqa: ANN001, ANN002
    with pytest.raises(rc.ChangeError) as caught:
        call(*args)
    return caught.value.errors


# ── Adding a model ───────────────────────────────────────────────────────────


def test_an_added_model_gets_its_family_from_the_provider_and_nothing_is_written(
    repo: Path, runs: Path
) -> None:
    before = registry_text(repo)
    draft = rc.draft_add_model(repo, runs, "google_ai_pro", "gemini-9-new", ["design_reasoning"])
    assert draft.kind == "add_model" and draft.summary == (
        "Add model gemini_9_new (google_ai_pro:gemini-9-new)"
    )
    assert (
        draft.diff.count("\n+") == 2 and "+  gemini_9_new: {provider: google_ai_pro" in draft.diff
    )
    assert "family: gemini" in draft.diff
    assert any("leaves this host" in n for n in draft.notes)
    assert any("You claim these capabilities" in n and "models test" in n for n in draft.notes)
    assert registry_text(repo) == before, "a draft changes no policy file"
    assert [p["id"] for p in rc.pending(runs)] == [draft.change_id]


def test_applying_writes_the_file_and_the_repository_still_validates(
    repo: Path, runs: Path
) -> None:
    draft = rc.draft_add_model(repo, runs, "google_ai_pro", "gemini-9-new", ["design_reasoning"])
    applied = rc.apply(repo, runs, draft.change_id)
    assert applied.path == "policy/model_registry.yaml"
    entry = registry(repo)["models"]["gemini_9_new"]
    assert entry["family"] == "gemini" and entry["capabilities"] == ["design_reasoning"]
    assert validate_repo(repo) == []
    assert rc.pending(runs) == []
    assert "# The model catalog." in registry_text(repo), "comments survive"


def test_a_model_without_capabilities_is_added_unclaimed_and_says_so(
    repo: Path, runs: Path
) -> None:
    draft = rc.draft_add_model(repo, runs, "google_ai_pro", "gemini-9-new", [])
    assert any("cannot be bound" in n for n in draft.notes)


@pytest.mark.parametrize(
    ("provider", "model_id", "capabilities", "expected"),
    [
        ("nope", "x", [], "is not a model provider"),
        ("deterministic_rules", "x", [], "is not a model provider"),
        ("llamacpp_local", "qwen3.5-other", [], "starts one fixed server"),
        ("google_ai_pro", "mystery-1", [], "no family pattern of google_ai_pro matches"),
        ("google_ai_pro", "gemini-9-new", ["telepathy"], "unknown capabilities ['telepathy']"),
        ("google_ai_pro", "", [], "short printable string"),
        ("google_ai_pro", "gemini-9\nnew", [], "short printable string"),
    ],
)
def test_an_add_that_cannot_be_made_is_refused_with_the_reason(
    repo: Path, runs: Path, provider: str, model_id: str, capabilities: list[str], expected: str
) -> None:
    errors = refused(rc.draft_add_model, repo, runs, provider, model_id, capabilities)
    assert any(expected in e for e in errors), errors
    assert rc.pending(runs) == []


def test_a_name_clash_is_refused(repo: Path, runs: Path) -> None:
    # a different id that would get the name of `gemini-3.8-flash-high`
    errors = refused(rc.draft_add_model, repo, runs, "google_ai_pro", "gemini-3.8.flash-high", [])
    assert errors == ["a model named 'gemini_3_8_flash_high' is already in the catalog"]


def test_a_known_model_is_not_added_twice_its_capabilities_are_claimed(
    repo: Path, runs: Path
) -> None:
    draft = rc.draft_add_model(
        repo, runs, "google_ai_pro", "gemini-4-argon-high", ["design_reasoning"]
    )
    assert draft.kind == "claim_capabilities"
    assert draft.summary == "Claim design_reasoning for gemini_4_argon_high"
    assert draft.diff.count("\n-  gemini_4_argon_high") == 1
    rc.apply(repo, runs, draft.change_id)
    assert registry(repo)["models"]["gemini_4_argon_high"]["capabilities"] == ["design_reasoning"]
    again = refused(
        rc.draft_add_model, repo, runs, "google_ai_pro", "gemini-4-argon-high", ["design_reasoning"]
    )
    assert any("already claims design_reasoning" in e for e in again)


def test_claims_add_to_the_capabilities_a_model_has(repo: Path, runs: Path) -> None:
    draft = rc.draft_add_model(repo, runs, "google_ai_pro", "gpt-oss-120b-medium", ["coordination"])
    rc.apply(repo, runs, draft.change_id)
    assert registry(repo)["models"]["gpt_oss_120b_medium"]["capabilities"] == [
        "design_reasoning",
        "coordination",
    ]


def test_naming_a_known_model_without_capabilities_is_refused(repo: Path, runs: Path) -> None:
    errors = refused(rc.draft_add_model, repo, runs, "google_ai_pro", "gemini-4-argon-high", [])
    assert any("already in the catalog" in e and "name capabilities to claim" in e for e in errors)


# ── Binding ──────────────────────────────────────────────────────────────────


def test_binding_a_slot_changes_one_line(repo: Path, runs: Path) -> None:
    rc.apply(
        repo,
        runs,
        rc.draft_add_model(
            repo, runs, "google_ai_pro", "gemini-4-argon-high", ["design_reasoning"]
        ).change_id,
    )
    draft = rc.draft_bind(repo, runs, "design.proposal_b.primary", ["gemini_4_argon_high"])
    assert (
        draft.summary
        == "Bind design.proposal_b.primary: gemini_4_argon_medium -> gemini_4_argon_high"
    )
    assert "-      primary: gemini_4_argon_medium" in draft.diff
    assert "+      primary: gemini_4_argon_high" in draft.diff
    assert any("A1.proposal_b" in n and "A2.critique_a_by_b" in n for n in draft.notes)
    assert any("leaves this host" in n for n in draft.notes)
    rc.apply(repo, runs, draft.change_id)
    assert registry(repo)["agents"]["design"]["proposal_b"]["primary"] == "gemini_4_argon_high"
    assert validate_repo(repo) == []


def test_a_bind_that_breaks_independence_is_refused_with_the_rules_own_message(
    repo: Path, runs: Path
) -> None:
    errors = refused(rc.draft_bind, repo, runs, "design.proposal_b.primary", ["claude_cli_default"])
    assert any(e.startswith("registry R14 A1.proposal_b") for e in errors), errors
    assert rc.pending(runs) == []


def test_a_bind_to_a_model_without_the_capability_is_refused(repo: Path, runs: Path) -> None:
    errors = refused(rc.draft_bind, repo, runs, "design.proposal_a.primary", ["clef_flash_local"])
    assert any(e.startswith("registry R18") for e in errors), errors


def test_a_list_slot_takes_a_list_and_a_scalar_slot_takes_one_model(repo: Path, runs: Path) -> None:
    draft = rc.draft_bind(
        repo, runs, "human_authority.model_assistance", ["claude_cli_default", "qwen3_5_9b"]
    )
    rc.apply(repo, runs, draft.change_id)
    assert registry(repo)["agents"]["human_authority"]["model_assistance"] == [
        "claude_cli_default",
        "qwen3_5_9b",
    ]
    errors = refused(
        rc.draft_bind, repo, runs, "build.primary", ["qwen3_5_9b", "claude_cli_default"]
    )
    assert any("takes one model" in e for e in errors)


@pytest.mark.parametrize(
    ("slot", "models", "expected"),
    [
        ("nope.primary", ["qwen3_5_9b"], "no slot 'nope.primary'"),
        ("design.synthesis.primary", ["qwen3_5_9b"], "is not a model"),
        ("human_authority.primary", ["qwen3_5_9b"], "is not a model"),
        ("build.primary", [], "one to eight distinct models"),
        ("build.primary", ["qwen3_5_9b", "qwen3_5_9b"], "one to eight distinct models"),
        ("build.primary", ["ghost"], "not in the catalog: ['ghost']"),
        ("build.primary", ["qwen3_5_9b"], "is already qwen3_5_9b"),
    ],
)
def test_a_bind_that_cannot_be_made_is_refused(
    repo: Path, runs: Path, slot: str, models: list[str], expected: str
) -> None:
    errors = refused(rc.draft_bind, repo, runs, slot, models)
    assert any(expected in e for e in errors), errors


# ── Removing ─────────────────────────────────────────────────────────────────


def test_a_bound_model_cannot_be_removed_until_it_is_unbound(repo: Path, runs: Path) -> None:
    errors = refused(rc.draft_remove_model, repo, runs, "qwen3_5_9b")
    assert errors == [
        "qwen3_5_9b is bound to grounding.primary, build.primary, release.explanation_fallback, "
        "learning.analysis_fallback, human_authority.model_assistance; bind those slots to "
        "another model with /agents first"
    ]


def test_an_unbound_model_is_removed_and_the_rest_is_untouched(repo: Path, runs: Path) -> None:
    before = registry(repo)
    draft = rc.draft_remove_model(repo, runs, "gemini_4_argon_low")
    assert draft.diff.count("\n-  gemini_4_argon_low") == 1 and "\n+  " not in draft.diff
    rc.apply(repo, runs, draft.change_id)
    after = registry(repo)
    del before["models"]["gemini_4_argon_low"]
    assert after == before and validate_repo(repo) == []


def test_the_coordinator_named_in_ln_agents_md_cannot_be_removed(repo: Path, runs: Path) -> None:
    (repo / "LN-AGENTS.md").write_text(
        "---\ncoordinator: gemini_4_argon_low\nfallback: gemini_4_argon_high\n---\nbody\n"
    )
    for name in ("gemini_4_argon_low", "gemini_4_argon_high"):
        assert any(
            "coordinator named in LN-AGENTS.md" in e
            for e in refused(rc.draft_remove_model, repo, runs, name)
        )


def test_removing_an_unknown_model_is_refused(repo: Path, runs: Path) -> None:
    assert refused(rc.draft_remove_model, repo, runs, "ghost") == [
        "'ghost' is not a model in the catalog"
    ]


# ── Confirming ───────────────────────────────────────────────────────────────


def test_a_draft_goes_stale_when_the_registry_changes_under_it(repo: Path, runs: Path) -> None:
    first = rc.draft_add_model(repo, runs, "google_ai_pro", "gemini-9-a", [])
    second = rc.draft_add_model(repo, runs, "google_ai_pro", "gemini-9-b", [])
    rc.apply(repo, runs, first.change_id)
    errors = refused(rc.apply, repo, runs, second.change_id)
    assert any("changed after change" in e and "draft it again" in e for e in errors)
    assert "gemini_9_b" not in registry(repo)["models"]


def test_a_hand_edit_of_the_registry_also_makes_a_draft_stale(repo: Path, runs: Path) -> None:
    draft = rc.draft_add_model(repo, runs, "google_ai_pro", "gemini-9-a", [])
    (repo / rc.REGISTRY).write_text(registry_text(repo) + "\n# edited by hand\n")
    assert any("changed after" in e for e in refused(rc.apply, repo, runs, draft.change_id))


def test_a_stored_draft_that_was_altered_is_validated_again_and_refused(
    repo: Path, runs: Path
) -> None:
    draft = rc.draft_add_model(repo, runs, "google_ai_pro", "gemini-9-a", [])
    path = runs / rc.CHANGES_DIR / f"{draft.change_id}.json"
    record = json.loads(path.read_text())
    broken = record["new_text"].replace(
        "family: gemini, capabilities: []}", "family: claude, capabilities: []}", 1
    )
    assert broken != record["new_text"]
    path.write_text(json.dumps({**record, "new_text": broken}))
    before = registry_text(repo)
    assert any("R17" in e for e in refused(rc.apply, repo, runs, draft.change_id))
    assert registry_text(repo) == before


@pytest.mark.parametrize("bad", ["../x", "abc", "ZZZZZZZZ", "0123456789", ""])
def test_only_a_well_formed_id_names_a_draft(repo: Path, runs: Path, bad: str) -> None:
    assert any("not a change id" in e for e in refused(rc.apply, repo, runs, bad))
    assert any("not a change id" in e for e in refused(rc.discard, runs, bad))


def test_an_unknown_draft_is_refused(repo: Path, runs: Path) -> None:
    assert refused(rc.apply, repo, runs, "deadbeef") == ["no pending change deadbeef"]


def test_discarding_drops_the_draft_and_writes_nothing(repo: Path, runs: Path) -> None:
    draft = rc.draft_add_model(repo, runs, "google_ai_pro", "gemini-9-a", [])
    before = registry_text(repo)
    assert rc.discard(runs, draft.change_id) == draft.summary
    assert rc.pending(runs) == [] and registry_text(repo) == before
    assert any("no pending change" in e for e in refused(rc.apply, repo, runs, draft.change_id))


def test_a_draft_can_only_be_applied_once(repo: Path, runs: Path) -> None:
    draft = rc.draft_add_model(repo, runs, "google_ai_pro", "gemini-9-a", [])
    rc.apply(repo, runs, draft.change_id)
    assert any("no pending change" in e for e in refused(rc.apply, repo, runs, draft.change_id))


def test_pending_drafts_list_oldest_first(repo: Path, runs: Path) -> None:
    a = rc.draft_add_model(repo, runs, "google_ai_pro", "gemini-9-a", [])
    b = rc.draft_add_model(repo, runs, "google_ai_pro", "gemini-9-b", [])
    assert [p["id"] for p in rc.pending(runs)] == [a.change_id, b.change_id]


# ── The slots ────────────────────────────────────────────────────────────────


def test_the_mapping_lists_every_slot_with_what_runs_on_it(repo: Path) -> None:
    rows = {r["slot"]: r for r in rc.mapping(repo)}
    proposal = rows["design.proposal_a.primary"]
    assert proposal["value"] == "claude_opus_5_5_high_cli" and proposal["rebindable"] is True
    assert proposal["operations"] == ["A1.proposal_a", "A2.critique_b_by_a"]
    assert proposal["spec"] == "agents/pipeline-design-agent.md"
    assert proposal["models"] == [
        {"name": "claude_opus_5_5_high_cli", "family": "claude", "location": "cloud"}
    ]
    assert rows["design.synthesis.primary"]["rebindable"] is False  # the orchestrator's own
    assert isinstance(rows["human_authority.model_assistance"]["value"], list)
    assert rows["build.primary"]["operations"] == [
        "C1.module_decomposition",
        "D1.test_generation",
        "E1.implementation_patch",
    ]
