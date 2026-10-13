"""Design-side gates (A4, B1, B2, B3, DR) against sealed artifacts from a real grounding run."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest
from test_grounding import Rig, git, repo, write_spec  # noqa: F401  (repo is a fixture)

from agentic_pipeline.gates import (
    GATE_HANDLERS,
    UNEVALUATED,
    VETO_EVALUATORS,
    GateError,
)
from agentic_pipeline.grounding import GROUNDING_HANDLERS
from agentic_pipeline.manifest import build_manifest, publish_stage_manifest, resolve_sealed_inputs
from agentic_pipeline.provenance import GENESIS_HASH
from agentic_pipeline.runner import Outcome, Runner
from agentic_pipeline.schemas import validate


def proposal(pid: str, context_hash: str, *, synthesized: bool = False, **extra: Any) -> dict:
    doc: dict[str, Any] = {
        "id": pid,
        "author_model": "test",
        "synthesized": synthesized,
        "topology": {"components": [{"name": "api"}]},
        "manifest_hash": context_hash,
    }
    if synthesized:
        doc["sources"] = {"api": "prop-a"}
    return {**doc, **extra}


class Design:
    """Grounding walked to G4, then A1/A3 outputs sealed by hand."""

    def __init__(self, repo: Path, tmp_path: Path, **spec_patch: Any) -> None:  # noqa: F811
        self.rig = Rig(repo, tmp_path, write_spec(tmp_path, **spec_patch) if spec_patch else None)
        self.runner: Runner = self.rig.runner()
        for _ in range(4):
            assert self.runner.step().outcome is Outcome.ADVANCED
        GROUNDING_HANDLERS["G4"](self.rig.ctx(self.runner, "G4"))
        inputs, errors = resolve_sealed_inputs(
            self.rig.run_dir, "demo", self.runner.registry, ["pinned_context_manifest"]
        )
        assert not errors
        path = inputs["pinned_context_manifest"].path / "context_manifest.json"
        self.context_hash = json.loads(path.read_text())["hash"]

    def seal(self, stage: str, artifacts: dict[str, tuple[str, dict | None]]) -> None:
        """artifacts: id -> (operation id, proposal doc or None for no file)."""
        built = {}
        for artifact, (operation, doc) in artifacts.items():
            root = self.rig.run_dir / "design" / operation
            root.mkdir(parents=True, exist_ok=True)
            for old in root.glob("*"):
                old.unlink()
            if doc is not None:
                (root / f"{operation}.json").write_text(json.dumps(doc))
            else:
                (root / "note.txt").write_text("no proposal json")
            built[artifact] = (root, True)
        manifest = build_manifest(
            self.rig.run_dir,
            run_id="demo",
            stage=stage,
            attempt="t",
            artifacts=built,
            operations={},
            provenance_tip=GENESIS_HASH,
            data_classes=dict.fromkeys(built, "internal"),
        )
        publish_stage_manifest(self.rig.run_dir, manifest)

    def seal_valid(self) -> None:
        h = self.context_hash
        self.seal(
            "A1",
            {
                "proposal_a_sealed": ("proposal_a", proposal("prop-a", h)),
                "proposal_b_sealed": ("proposal_b", proposal("prop-b", h)),
            },
        )
        self.seal(
            "A3", {"synthesis_sealed": ("synthesis", proposal("prop-s", h, synthesized=True))}
        )

    def guard(self, node: str) -> str:
        return GATE_HANDLERS[node](self.rig.ctx(self.runner, node))

    def records(self, node: str) -> list[dict]:
        folder = self.rig.run_dir / "gates" / node
        return [json.loads(p.read_text()) for p in sorted(folder.glob("*.json"))]


@pytest.fixture
def design(repo: Path, tmp_path: Path) -> Design:  # noqa: F811
    return Design(repo, tmp_path)


def test_valid_design_walks_a4_to_human_review(design: Design) -> None:
    design.seal_valid()
    design.runner.state.current = "A4"
    design.runner._save()
    seen = []
    for _ in range(4):  # A4, B1, B2, B3 each decide; B3 sends the run to H1
        result = design.runner.step()
        assert result.outcome is Outcome.ADVANCED, result
        seen.append(design.runner.state.current)
    assert seen == ["B1", "B2", "B3", "H1"]
    assert design.records("A4")[0]["guard"] == "all_required_valid"


def test_a4_never_reports_a_conflict_it_cannot_define(design: Design) -> None:
    design.seal_valid()
    assert design.guard("A4") == "all_required_valid"
    assert design.records("A4")[0]["constraint_sources_conflict"] == UNEVALUATED


@pytest.mark.parametrize(
    ("which", "mutate", "guard"),
    [
        ("proposal_a_sealed", lambda d: d.pop("topology"), "proposal_invalid"),
        (
            "proposal_b_sealed",
            lambda d: d.update(manifest_hash="sha256:" + "0" * 64),
            "proposal_invalid",
        ),
        (
            "proposal_a_sealed",
            lambda d: d.update(synthesized=True, sources={"x": "y"}),
            "proposal_invalid",
        ),
        ("proposal_b_sealed", lambda d: d.update(id="prop-a"), "proposal_invalid"),
        ("synthesis_sealed", lambda d: d.update(synthesized=False), "synthesis_invalid"),
        (
            "synthesis_sealed",
            lambda d: d.update(sources={"api": "someone-else"}),
            "synthesis_invalid",
        ),
        (
            "synthesis_sealed",
            lambda d: d.update(manifest_hash="sha256:" + "1" * 64),
            "synthesis_invalid",
        ),
    ],
    ids=[
        "no-topology",
        "wrong-manifest-hash",
        "proposal-claims-synthesis",
        "duplicate-id",
        "synthesis-not-marked",
        "foreign-source",
        "synthesis-wrong-hash",
    ],
)
def test_a4_guards(design: Design, which: str, mutate: Any, guard: str) -> None:
    h = design.context_hash
    docs = {
        "proposal_a_sealed": proposal("prop-a", h),
        "proposal_b_sealed": proposal("prop-b", h),
        "synthesis_sealed": proposal("prop-s", h, synthesized=True),
    }
    mutate(docs[which])
    design.seal("A1", {k: (k.removesuffix("_sealed"), docs[k]) for k in list(docs)[:2]})
    design.seal("A3", {"synthesis_sealed": ("synthesis", docs["synthesis_sealed"])})
    assert design.guard("A4") == guard


def test_a4_prefers_proposal_invalid_over_synthesis_invalid(design: Design) -> None:
    h = design.context_hash
    bad = proposal("prop-a", h)
    del bad["topology"]
    design.seal(
        "A1",
        {
            "proposal_a_sealed": ("proposal_a", bad),
            "proposal_b_sealed": ("proposal_b", proposal("prop-b", h)),
        },
    )
    design.seal("A3", {"synthesis_sealed": ("synthesis", None)})
    assert design.guard("A4") == "proposal_invalid"


def test_a4_text_only_output_is_invalid_not_a_crash(design: Design) -> None:
    h = design.context_hash
    design.seal(
        "A1",
        {
            "proposal_a_sealed": ("proposal_a", None),
            "proposal_b_sealed": ("proposal_b", proposal("prop-b", h)),
        },
    )
    design.seal("A3", {"synthesis_sealed": ("synthesis", proposal("prop-s", h, synthesized=True))})
    assert design.guard("A4") == "proposal_invalid"
    assert "proposal_a.json" in json.dumps(design.records("A4")[0]["problems"])


def test_a4_blocks_when_a_sealed_input_is_missing_or_tampered(design: Design) -> None:
    with pytest.raises(GateError, match="no published manifest"):
        design.guard("A4")
    design.seal_valid()
    (design.rig.run_dir / "design" / "proposal_a" / "proposal_a.json").write_text("{}")
    with pytest.raises(GateError, match="content changed"):
        design.guard("A4")


def test_a_rerun_after_a_retry_sees_the_new_inputs(design: Design) -> None:
    h = design.context_hash
    design.seal_valid()
    assert design.guard("A4") == "all_required_valid"
    bad = proposal("prop-a", h)
    del bad["topology"]
    design.seal(
        "A1",
        {
            "proposal_a_sealed": ("proposal_a", bad),
            "proposal_b_sealed": ("proposal_b", proposal("prop-b", h)),
        },
    )
    assert design.guard("A4") == "proposal_invalid"  # not the earlier answer
    guards = sorted(r["guard"] for r in design.records("A4"))
    assert guards == ["all_required_valid", "proposal_invalid"]  # one record per input set


def test_b1_lists_unevaluated_vetoes_and_does_not_clear_them(design: Design) -> None:
    design.seal_valid()
    assert design.guard("B1") == "at_least_one_survivor"
    record = design.records("B1")[0]
    assert record["evaluated"] == ["schema_invalid"]
    assert set(record["unevaluated"]) == {
        "violates_accepted_adr",
        "data_class_violation",
        "known_vulnerable_dependency",
        "contract_break",
    }
    assert record["fired"] == {a: [] for a in record["fired"]}


def test_b1_schema_veto_fires_per_candidate(design: Design) -> None:
    h = design.context_hash
    bad = proposal("prop-a", h)
    del bad["topology"]
    design.seal(
        "A1",
        {
            "proposal_a_sealed": ("proposal_a", bad),
            "proposal_b_sealed": ("proposal_b", proposal("prop-b", h)),
        },
    )
    design.seal("A3", {"synthesis_sealed": ("synthesis", proposal("prop-s", h, synthesized=True))})
    assert design.guard("B1") == "at_least_one_survivor"
    record = design.records("B1")[0]
    assert record["fired"]["proposal_a_sealed"] == ["schema_invalid"]
    assert record["survivors"] == ["proposal_b_sealed", "synthesis_sealed"]


def test_b1_all_vetoed_is_not_correctable(design: Design) -> None:
    h = design.context_hash
    bad = lambda pid, **kw: {k: v for k, v in proposal(pid, h, **kw).items() if k != "topology"}  # noqa: E731
    design.seal(
        "A1",
        {
            "proposal_a_sealed": ("proposal_a", bad("prop-a")),
            "proposal_b_sealed": ("proposal_b", bad("prop-b")),
        },
    )
    design.seal("A3", {"synthesis_sealed": ("synthesis", bad("prop-s", synthesized=True))})
    assert design.guard("B1") == "all_vetoed_otherwise"


def test_a_veto_evaluator_must_name_a_policy_veto(
    design: Design, monkeypatch: pytest.MonkeyPatch
) -> None:
    design.seal_valid()
    monkeypatch.setitem(VETO_EVALUATORS, "ghost_veto", lambda doc: False)
    with pytest.raises(GateError, match="ghost_veto"):
        design.guard("B1")


def test_b2_publishes_an_explicit_unscored_record(design: Design) -> None:
    design.seal_valid()
    assert design.guard("B2") == "always"
    inputs, errors = resolve_sealed_inputs(
        design.rig.run_dir, "demo", design.runner.registry, ["pinned_context_manifest"]
    )
    assert not errors
    from agentic_pipeline.manifest import load_manifest

    manifest = load_manifest(design.rig.run_dir, "B2", "demo")
    assert manifest is not None
    entry = manifest["artifacts"]["scores"]
    assert entry["data_class"] == "internal" and entry["sealed"] is True
    scores = json.loads((design.rig.run_dir / entry["path"] / "scores.json").read_text())
    validate("scores", scores)
    assert scores["scored"] is False and scores["values"] == []
    assert scores["flags"]["high_uncertainty"] is True


def test_b3_never_fast_paths_with_nothing_evaluable(design: Design) -> None:
    design.seal_valid()
    assert design.guard("B3") == "otherwise"
    results = design.records("B3")[0]["require_all"]
    assert results["task_class_on_allowlist"] == "failed"  # allowlist is empty
    assert results["no_active_veto"] == "failed"  # four vetoes unevaluated: never cleared
    assert results["no_high_uncertainty"] == "failed"
    assert results["no_near_tie"] == UNEVALUATED
    assert results["no_security_tag"] == UNEVALUATED  # no evaluator: fails closed
    assert results["contracts_and_protected_tests_intact"] == "passed"


def test_b3_stays_on_the_human_path_even_if_the_class_is_allowlisted(
    repo: Path,  # noqa: F811
    tmp_path: Path,
) -> None:
    path = repo / "policy" / "fast_path.yaml"
    path.write_text(
        path.read_text().replace("task_class_allowlist: []", "task_class_allowlist: [api_addition]")
    )
    git(repo, "commit", "-qam", "allow")
    d = Design(repo, tmp_path)
    d.seal_valid()
    assert d.guard("B3") == "otherwise"
    assert d.records("B3")[0]["require_all"]["task_class_on_allowlist"] == "passed"


def test_b3_notices_tampered_grounding(design: Design) -> None:
    design.seal_valid()
    (design.rig.run_dir / "grounding" / "G1" / "protected_tests" / "test_x.py").write_text("evil")
    assert design.guard("B3") == "otherwise"
    assert (
        design.records("B3")[0]["require_all"]["contracts_and_protected_tests_intact"] == "failed"
    )


def test_dr_refuses_to_invent_a_winner(design: Design) -> None:
    with pytest.raises(GateError, match="scored winner"):
        design.guard("DR")


def test_gates_refuse_after_policy_drift(design: Design) -> None:
    design.seal_valid()
    path = design.rig.repo / "policy" / "vetoes.yaml"
    path.write_text(path.read_text() + "# edited\n")
    with pytest.raises(GateError, match="changed since the run started"):
        design.guard("B1")


def test_the_spent_route_is_never_chosen_by_a_gate(design: Design) -> None:
    design.seal_valid()
    for node in ("A4", "B1", "B3"):
        assert design.guard(node) not in {"design_spent", "constraint_sources_conflict"}
    assert copy.deepcopy(GATE_HANDLERS).keys() == {"A4", "B1", "B2", "B3", "DR"}
