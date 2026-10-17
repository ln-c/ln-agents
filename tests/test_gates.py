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
    approval_evidence,
)
from agentic_pipeline.grounding import GROUNDING_HANDLERS
from agentic_pipeline.manifest import build_manifest, publish_stage_manifest, resolve_sealed_inputs
from agentic_pipeline.provenance import GENESIS_HASH
from agentic_pipeline.run_state import load_policy
from agentic_pipeline.runner import Outcome, Runner
from agentic_pipeline.schemas import validate

# Recorded backend per operation (the registry's A1/A2 assignment): independent by design.
BACKENDS = {
    "proposal_a": "claude_cli_default",
    "proposal_b": "gemini_3_8_flash_high",
    "synthesis": "claude_cli_default",
    "critique_b_by_a": "claude_cli_default",
    "critique_a_by_b": "gemini_3_8_flash_high",
    "synthesis_review": "gemini_3_8_flash_high",  # the synthesis ran on claude_cli_default
}


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


def blocking(fid: str, pid: str) -> dict:
    return {
        "id": fid,
        "severity": "blocking",
        "description": "stores secrets in the clear",
        "proposal_id": pid,
        "subject": "api",
        "impact": "credential disclosure",
        "violated": "security constraint S-1",
        "evidence": ["api component writes config.secret to disk unencrypted"],
        "remediation": "encrypt at rest",
    }


CRITERIA = ("security", "reliability", "scalability", "maintainability", "cost")


def scores(value: int = 3, **override: int | None) -> dict:
    """A critique `scores` block: every criterion at `value`, or dropped when overridden to None."""
    given = {c: override.get(c, value) for c in CRITERIA}
    return {c: {"score": v, "evidence": f"{c} evidence"} for c, v in given.items() if v is not None}


def critique(
    cid: str, target: str, context_hash: str, findings: list | None = None, **extra: Any
) -> dict:
    findings = findings or []
    counts = {"blocking": 0, "non_blocking": 0, "informational": 0}
    for finding in findings:
        counts[finding["severity"]] += 1
    doc = {
        "id": cid,
        "author_model": "test",
        "target_proposal_id": target,
        "manifest_hash": context_hash,
        "review_complete": True,
        "counts": counts,
        "findings": findings,
    }
    return {**doc, **extra}


class Design:
    """Grounding walked to G, then A1/A3 outputs sealed by hand."""

    def __init__(self, repo: Path, tmp_path: Path, **spec_patch: Any) -> None:  # noqa: F811
        self.rig = Rig(repo, tmp_path, write_spec(tmp_path, **spec_patch) if spec_patch else None)
        self.runner: Runner = self.rig.runner()
        assert self.runner.step().outcome is Outcome.ADVANCED  # START
        GROUNDING_HANDLERS["G"](self.rig.ctx(self.runner, "G"))
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
            operations={
                op: {"backend": BACKENDS[op], "invocation_id": f"i-{op}", "status": "ok"}
                for op, _ in artifacts.values()
            },
            provenance_tip=GENESIS_HASH,
            data_classes=dict.fromkeys(built, "internal"),
        )
        publish_stage_manifest(self.rig.run_dir, manifest)

    def seal_proposals(self) -> None:
        h = self.context_hash
        self.seal(
            "A1",
            {
                "proposal_a_sealed": ("proposal_a", proposal("prop-a", h)),
                "proposal_b_sealed": ("proposal_b", proposal("prop-b", h)),
            },
        )

    def seal_valid(self) -> None:
        h = self.context_hash
        self.seal_proposals()
        self.seal(
            "A3", {"synthesis_sealed": ("synthesis", proposal("prop-s", h, synthesized=True))}
        )

    def seal_critiques(self, a_on_b: dict | None = None, b_on_a: dict | None = None) -> None:
        h = self.context_hash
        self.seal(
            "A2",
            {
                "critique_b_by_a_sealed": (
                    "critique_b_by_a",
                    a_on_b or critique("c1", "prop-b", h),
                ),
                "critique_a_by_b_sealed": (
                    "critique_a_by_b",
                    b_on_a or critique("c2", "prop-a", h),
                ),
            },
        )

    def seal_reviews(
        self, a: dict | None = None, b: dict | None = None, s: dict | None = None
    ) -> None:
        """Seal A2's critiques and A3R's synthesis review, each carrying `scores` blocks."""
        h = self.context_hash
        self.seal_critiques(
            critique("c1", "prop-b", h, scores=b if b is not None else scores(1)),
            critique("c2", "prop-a", h, scores=a if a is not None else scores(3)),
        )
        self.seal(
            "A3R",
            {
                "synthesis_review_sealed": (
                    "synthesis_review",
                    critique("c3", "prop-s", h, scores=s if s is not None else scores(4)),
                )
            },
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
    design.seal_reviews()
    design.runner.state.current = "A4"
    design.runner._save()
    result = design.runner.step()  # A4 hands the validated design to the A3R review stage
    assert result.outcome is Outcome.BLOCKED and "dispatch_stage" in result.detail
    design.runner.state.current = "B1"  # the review is sealed by hand: no model runs here
    design.runner._save()
    seen = []
    for _ in range(3):  # B1, B2, B3 each decide; B3 sends the run to H1
        result = design.runner.step()
        assert result.outcome is Outcome.ADVANCED, result
        seen.append(design.runner.state.current)
    assert seen == ["B2", "B3", "H1"]
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
    assert record["evaluated"] == [
        "contract_break",
        "data_class_violation",
        "known_vulnerable_dependency",
        "schema_invalid",
    ]
    assert record["unevaluated"] == []
    assert record["fired"] == {a: [] for a in record["fired"]}
    # None of the test proposals declares dependencies, data flows or contracts touched.
    every = ["contract_break", "data_class_violation", "known_vulnerable_dependency"]
    assert all(sorted(v) == every for v in record["undetermined"].values())


SNAPSHOT = """version: 1
source: osv
generated_at: 2026-10-14
ecosystems: [pypi]
advisories:
  - id: GHSA-test-0001
    severity: critical
    affected:
      - package: {ecosystem: PyPI, name: badlib}
        ranges: [{type: ECOSYSTEM, events: [{introduced: "0"}, {fixed: "2.0"}]}]
"""


def with_snapshot(repo: Path, tmp_path: Path) -> Design:  # noqa: F811
    (repo / "policy" / "advisories.yaml").write_text(SNAPSHOT)
    git(repo, "commit", "-qam", "snapshot")
    return Design(repo, tmp_path)


def seal_with_dependencies(d: Design, a: list | None, b: list | None, s: list | None) -> None:
    h = d.context_hash

    def prop(pid: str, deps: list | None, **extra: Any) -> dict:
        given = {"dependencies": deps} if deps is not None else {}
        return proposal(pid, h, data_flows=[], contracts_touched=[], **given, **extra)

    d.seal(
        "A1",
        {
            "proposal_a_sealed": ("proposal_a", prop("prop-a", a)),
            "proposal_b_sealed": ("proposal_b", prop("prop-b", b)),
        },
    )
    d.seal("A3", {"synthesis_sealed": ("synthesis", prop("prop-s", s, synthesized=True))})


def test_b1_vetoes_a_declared_vulnerable_dependency(
    repo: Path,  # noqa: F811
    tmp_path: Path,
) -> None:
    d = with_snapshot(repo, tmp_path)
    bad = [{"name": "BadLib", "ecosystem": "pypi", "version": "1.5"}]
    good = [{"name": "badlib", "ecosystem": "pypi", "version": "2.1"}]
    seal_with_dependencies(d, bad, [], good)
    assert d.guard("B1") == "at_least_one_survivor"
    record = d.records("B1")[0]
    assert record["fired"]["proposal_a_sealed"] == ["known_vulnerable_dependency"]
    assert record["survivors"] == ["proposal_b_sealed", "synthesis_sealed"]
    assert record["undetermined"] == {a: [] for a in record["undetermined"]}


def test_b1_never_clears_a_dependency_it_cannot_check(
    repo: Path,  # noqa: F811
    tmp_path: Path,
) -> None:
    d = with_snapshot(repo, tmp_path)
    npm = [{"name": "left-pad", "ecosystem": "npm", "version": "1.0.0"}]
    unversioned = [{"name": "badlib", "ecosystem": "pypi"}]
    seal_with_dependencies(d, npm, unversioned, None)
    d.guard("B1")
    record = d.records("B1")[0]
    assert record["fired"] == {a: [] for a in record["fired"]}
    assert all(v == ["known_vulnerable_dependency"] for v in record["undetermined"].values())


def test_an_empty_snapshot_never_clears_a_declared_dependency(design: Design) -> None:
    deps = [{"name": "requests", "ecosystem": "pypi", "version": "2.32.0"}]
    seal_with_dependencies(design, deps, [], [])
    design.guard("B1")
    undetermined = design.records("B1")[0]["undetermined"]
    assert undetermined["proposal_a_sealed"] == ["known_vulnerable_dependency"]
    assert undetermined["proposal_b_sealed"] == []  # declared none: clear


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


def published_scores(design: Design) -> dict:
    assert design.guard("B2") == "always"
    from agentic_pipeline.manifest import load_manifest

    manifest = load_manifest(design.rig.run_dir, "B2", "demo")
    assert manifest is not None
    entry = manifest["artifacts"]["scores"]
    assert entry["data_class"] == "internal" and entry["sealed"] is True
    doc = json.loads((design.rig.run_dir / entry["path"] / "scores.json").read_text())
    validate("scores", doc)
    return doc


def test_b2_with_no_reviews_is_unscored_and_uncertain(design: Design) -> None:
    design.seal_valid()
    doc = published_scores(design)
    assert doc["scored"] is False and doc["ranking"] == []
    assert doc["flags"] == {"near_tie": True, "high_uncertainty": True}
    assert all(c["reasons"] for c in doc["candidates"].values())


def test_b2_uses_the_critics_zero_to_four_scores_as_given(design: Design) -> None:
    design.seal_valid()
    design.seal_reviews(a=scores(4), b=scores(0), s=scores(2))
    doc = published_scores(design)
    by = {(v["candidate"], v["criterion"]): v["value"] for v in doc["values"]}
    assert by[("proposal_a_sealed", "security")] == 4
    assert by[("proposal_b_sealed", "security")] == 0
    assert by[("synthesis_sealed", "security")] == 2
    assert by[("proposal_a_sealed", "all")] == 0  # fully scored: no uncertainty
    weighted = {c: d["weighted"] for c, d in doc["candidates"].items()}
    assert weighted["proposal_a_sealed"] == pytest.approx(1.0)
    assert weighted["proposal_b_sealed"] == pytest.approx(0.0)
    assert weighted["synthesis_sealed"] == pytest.approx(0.5)
    assert doc["ranking"] == ["proposal_a_sealed", "synthesis_sealed", "proposal_b_sealed"]
    assert doc["flags"] == {"near_tie": False, "high_uncertainty": False}


def test_b2_flags_a_near_tie(design: Design) -> None:
    design.seal_valid()
    design.seal_reviews(a=scores(3), b=scores(3), s=scores(1))
    assert published_scores(design)["flags"]["near_tie"] is True


def test_a_missing_score_is_unscored_and_raises_uncertainty(design: Design) -> None:
    design.seal_valid()
    design.seal_reviews(a=scores(3, cost=None), b=scores(2), s=scores(4))
    doc = published_scores(design)
    a = doc["candidates"]["proposal_a_sealed"]
    assert a["unscored_criteria"] == ["cost"] and a["weighted"] is None
    assert "proposal_a_sealed" not in doc["ranking"]
    assert doc["flags"]["near_tie"] is False  # the other two are still comparable


def test_an_evidence_free_score_is_refused_by_the_schema() -> None:
    bad = {"security": {"score": 4, "evidence": ""}}
    with pytest.raises(Exception, match="evidence|non-empty|short"):
        validate("critique", critique("c", "p", "sha256:" + "0" * 64, scores=bad))
    with pytest.raises(Exception, match="smaller than or equal to 4"):
        validate("critique", critique("c", "p", "sha256:" + "0" * 64, scores=scores(5)))


def test_a_review_on_the_synthesizers_backend_is_not_counted(
    design: Design, monkeypatch: pytest.MonkeyPatch
) -> None:
    design.seal_valid()
    monkeypatch.setitem(BACKENDS, "synthesis_review", "claude_cli_default")  # same as the synthesis
    design.seal_reviews()
    doc = published_scores(design)
    s = doc["candidates"]["synthesis_sealed"]
    assert s["weighted"] is None and "same backend" in s["reasons"][0]
    assert doc["flags"]["high_uncertainty"] is True  # one of three candidates fully unscored


def test_a_review_of_another_proposal_is_not_counted(design: Design) -> None:
    design.seal_valid()
    design.seal_reviews()
    h = design.context_hash
    design.seal(
        "A3R",
        {
            "synthesis_review_sealed": (
                "synthesis_review",
                critique("c3", "prop-a", h, scores=scores(4)),
            )
        },
    )
    s = published_scores(design)["candidates"]["synthesis_sealed"]
    assert s["weighted"] is None and "target_proposal_id" in s["reasons"][0]


def test_a_blocking_finding_against_the_synthesis_marks_it_contested(design: Design) -> None:
    design.seal_valid()
    design.seal_reviews()
    h = design.context_hash
    design.seal(
        "A3R",
        {
            "synthesis_review_sealed": (
                "synthesis_review",
                critique("c3", "prop-s", h, [blocking("f1", "prop-s")], scores=scores(4)),
            )
        },
    )
    doc = published_scores(design)
    assert doc["candidates"]["synthesis_sealed"]["contested"] is True
    assert doc["flags"]["high_uncertainty"] is True


def test_a_tampered_review_blocks_b2(design: Design) -> None:
    design.seal_valid()
    design.seal_reviews()
    (design.rig.run_dir / "design" / "synthesis_review" / "synthesis_review.json").write_text("{}")
    with pytest.raises(GateError, match="content changed"):
        design.guard("B2")


def test_b3_never_fast_paths_with_nothing_evaluable(design: Design) -> None:
    design.seal_valid()
    assert design.guard("B3") == "otherwise"
    results = design.records("B3")[0]["require_all"]
    assert results["task_class_on_allowlist"] == "failed"  # allowlist is empty
    assert results["no_active_veto"] == "failed"  # four vetoes unevaluated: never cleared
    assert results["no_high_uncertainty"] == "failed"
    assert results["no_near_tie"] == UNEVALUATED  # nothing scored
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
    (design.rig.run_dir / "grounding" / "G" / "protected_tests" / "test_x.py").write_text("evil")
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
    assert copy.deepcopy(GATE_HANDLERS).keys() == {"A2", "A4", "B1", "B2", "B3", "DR"}


# ── A2 critiques ─────────────────────────────────────────────────────────────


def a2_ready(design: Design, **kw: Any) -> None:
    design.seal_valid()
    design.seal_critiques(**kw)


def test_clean_critiques_allow_synthesis(design: Design) -> None:
    a2_ready(design)
    assert design.guard("A2") == "no_blocking_flaw"
    assert design.records("A2")[0]["blocking_findings"] == {
        "critique_b_by_a": 0,
        "critique_a_by_b": 0,
    }


def test_non_blocking_findings_do_not_stop_synthesis(design: Design) -> None:
    h = design.context_hash
    note = {"id": "f1", "severity": "non_blocking", "description": "verbose naming"}
    a2_ready(design, a_on_b=critique("c1", "prop-b", h, [note]))
    assert design.guard("A2") == "no_blocking_flaw"


@pytest.mark.parametrize("side", ["a_on_b", "b_on_a", "both"])
def test_a_blocking_finding_in_either_critique_is_a_flaw(design: Design, side: str) -> None:
    h = design.context_hash
    on_b = critique("c1", "prop-b", h, [blocking("f1", "prop-b")])
    on_a = critique("c2", "prop-a", h, [blocking("f2", "prop-a")])
    kw = (
        {"a_on_b": on_b}
        if side == "a_on_b"
        else {"b_on_a": on_a}
        if side == "b_on_a"
        else {"a_on_b": on_b, "b_on_a": on_a}
    )
    a2_ready(design, **kw)
    assert design.guard("A2") == "blocking_flaw"


def _drop_evidence(doc: dict) -> None:
    del doc["findings"][0]["evidence"]


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (_drop_evidence, "schema"),
        (lambda d: d.update(review_complete=False), "review_complete"),
        (lambda d: d["counts"].update(blocking=0), "counts"),
        (lambda d: d.update(manifest_hash="sha256:" + "0" * 64), "manifest_hash"),
        (lambda d: d.update(target_proposal_id="prop-a"), "target_proposal_id"),
        (lambda d: d["findings"][0].update(proposal_id="prop-a"), "different proposal"),
    ],
    ids=[
        "no-evidence",
        "incomplete",
        "wrong-counts",
        "wrong-hash",
        "wrong-target",
        "foreign-finding",
    ],
)
def test_an_unusable_critique_blocks_instead_of_reading_as_clean(
    design: Design, mutate: Any, message: str
) -> None:
    h = design.context_hash
    bad = critique("c1", "prop-b", h, [blocking("f1", "prop-b")])
    mutate(bad)
    a2_ready(design, a_on_b=bad)
    with pytest.raises(GateError, match=message):
        design.guard("A2")


def test_a_critic_on_the_proposals_backend_is_a_self_critique(
    design: Design, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(BACKENDS, "critique_b_by_a", "gemini_3_8_flash_high")  # same as proposal_b
    a2_ready(design)
    with pytest.raises(GateError, match="same backend"):
        design.guard("A2")


def _extra_model(design: Design, name: str, family: str) -> None:
    design.runner.registry["models"][name] = {
        "provider": "google_ai_pro",
        "model_id": name,
        "family": family,
        "capabilities": ["design_reasoning"],
    }


def test_a_critic_on_another_model_of_the_proposals_family_is_a_self_critique(
    design: Design, monkeypatch: pytest.MonkeyPatch
) -> None:
    _extra_model(design, "gemini_elsewhere", "gemini")  # not proposal_b's model id
    monkeypatch.setitem(BACKENDS, "critique_b_by_a", "gemini_elsewhere")
    a2_ready(design)
    with pytest.raises(GateError, match="same backend or model family"):
        design.guard("A2")


def test_a_critic_that_is_not_a_catalog_model_is_not_granted_independence(
    design: Design, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(BACKENDS, "critique_b_by_a", "mystery")  # no such model
    a2_ready(design)
    with pytest.raises(GateError, match="no resolvable model family"):
        design.guard("A2")


def test_a_text_only_critique_blocks(design: Design) -> None:
    design.seal_valid()
    h = design.context_hash
    design.seal(
        "A2",
        {
            "critique_b_by_a_sealed": ("critique_b_by_a", None),
            "critique_a_by_b_sealed": ("critique_a_by_b", critique("c2", "prop-a", h)),
        },
    )
    with pytest.raises(GateError, match="critique_b_by_a.json"):
        design.guard("A2")


def test_a2_guards_are_real_edges_and_never_the_spent_route(design: Design) -> None:
    edges = {e.guard for e in design.runner.graph.edges if e.src == "A2"}
    assert {"blocking_flaw", "no_blocking_flaw"} <= edges
    a2_ready(design)
    assert design.guard("A2") != "design_spent"


def test_a2_rerun_sees_new_critiques(design: Design) -> None:
    h = design.context_hash
    a2_ready(design)
    assert design.guard("A2") == "no_blocking_flaw"
    design.seal_critiques(a_on_b=critique("c1", "prop-b", h, [blocking("f1", "prop-b")]))
    assert design.guard("A2") == "blocking_flaw"
    assert len(design.records("A2")) == 2


def test_critique_schema_requires_evidence_only_for_blocking() -> None:
    h = "sha256:" + "0" * 64
    info = {"id": "f", "severity": "informational", "description": "note"}
    validate("critique", critique("c", "p", h, [info]))
    thin = {"id": "f", "severity": "blocking", "description": "bad"}
    from agentic_pipeline.schemas import ValidationError

    with pytest.raises(ValidationError):
        validate("critique", critique("c", "p", h, [thin]))


def flow(fid: str, data_class: str, destination: str) -> dict:
    return {"id": fid, "data_class": data_class, "destination": destination}


def seal_with_flows(d: Design, a: list | None, b: list | None, s: list | None) -> None:
    h = d.context_hash

    def prop(pid: str, flows: list | None, **extra: Any) -> dict:
        given = {"data_flows": flows} if flows is not None else {}
        return proposal(pid, h, dependencies=[], contracts_touched=[], **given, **extra)

    d.seal(
        "A1",
        {
            "proposal_a_sealed": ("proposal_a", prop("prop-a", a)),
            "proposal_b_sealed": ("proposal_b", prop("prop-b", b)),
        },
    )
    d.seal("A3", {"synthesis_sealed": ("synthesis", prop("prop-s", s, synthesized=True))})


def test_b1_vetoes_confidential_data_sent_to_the_cloud(design: Design) -> None:
    seal_with_flows(
        design,
        [flow("f1", "confidential", "cloud_service")],
        [flow("f1", "confidential", "internal_service"), flow("f2", "public", "third_party")],
        [],
    )
    design.guard("B1")
    record = design.records("B1")[0]
    assert record["fired"]["proposal_a_sealed"] == ["data_class_violation"]
    assert record["survivors"] == ["proposal_b_sealed", "synthesis_sealed"]
    assert record["undetermined"] == {a: [] for a in record["undetermined"]}


def test_b1_does_not_clear_a_design_that_omits_its_data_flows(design: Design) -> None:
    seal_with_flows(design, None, [], [])
    design.guard("B1")
    record = design.records("B1")[0]
    assert record["undetermined"]["proposal_a_sealed"] == ["data_class_violation"]
    assert record["undetermined"]["proposal_b_sealed"] == []


def test_a_proposal_cannot_label_data_below_the_entry_specs_classification(
    repo: Path,  # noqa: F811
    tmp_path: Path,
) -> None:
    d = Design(repo, tmp_path, data__declared_classification="confidential")
    seal_with_flows(d, [flow("f1", "public", "cloud_service")], [], [])
    d.guard("B1")
    assert d.records("B1")[0]["fired"]["proposal_a_sealed"] == ["data_class_violation"]


def test_unresolved_classification_makes_a_cloud_flow_undetermined(
    repo: Path,  # noqa: F811
    tmp_path: Path,
) -> None:
    d = Design(repo, tmp_path, data__unresolved_classification=["customer export"])
    seal_with_flows(d, [flow("f1", "internal", "cloud_service")], [], [])
    d.guard("B1")
    assert d.records("B1")[0]["undetermined"]["proposal_a_sealed"] == ["data_class_violation"]


def seal_with_contracts(d: Design, a: list | None, b: list | None, s: list | None) -> None:
    h = d.context_hash

    def prop(pid: str, touched: list | None, **extra: Any) -> dict:
        given = {"contracts_touched": touched} if touched is not None else {}
        return proposal(pid, h, dependencies=[], data_flows=[], **given, **extra)

    d.seal(
        "A1",
        {
            "proposal_a_sealed": ("proposal_a", prop("prop-a", a)),
            "proposal_b_sealed": ("proposal_b", prop("prop-b", b)),
        },
    )
    d.seal("A3", {"synthesis_sealed": ("synthesis", prop("prop-s", s, synthesized=True))})


def with_contract(repo: Path, tmp_path: Path) -> Design:  # noqa: F811
    (repo / "contracts").mkdir()
    (repo / "contracts" / "api.yaml").write_text("version: 1\n")
    git(repo, "add", "contracts")
    git(repo, "commit", "-qm", "contract")
    return Design(repo, tmp_path)


def touch(path: str, change: str) -> dict:
    return {"path": path, "change": change}


def test_b1_vetoes_a_breaking_change_to_a_pinned_contract(
    repo: Path,  # noqa: F811
    tmp_path: Path,
) -> None:
    d = with_contract(repo, tmp_path)
    seal_with_contracts(
        d,
        [touch("contracts/api.yaml", "breaking")],
        [touch("contracts/api.yaml", "compatible")],
        [],
    )
    d.guard("B1")
    record = d.records("B1")[0]
    assert record["fired"]["proposal_a_sealed"] == ["contract_break"]
    assert record["survivors"] == ["proposal_b_sealed", "synthesis_sealed"]
    assert record["undetermined"] == {a: [] for a in record["undetermined"]}


def test_b1_does_not_clear_unpinned_or_unstated_contract_changes(
    repo: Path,  # noqa: F811
    tmp_path: Path,
) -> None:
    d = with_contract(repo, tmp_path)
    seal_with_contracts(d, None, [touch("contracts/ghost.yaml", "breaking")], [])
    d.guard("B1")
    record = d.records("B1")[0]
    assert record["undetermined"]["proposal_a_sealed"] == ["contract_break"]
    assert record["undetermined"]["proposal_b_sealed"] == ["contract_break"]
    assert record["fired"]["proposal_b_sealed"] == []
    assert record["undetermined"]["synthesis_sealed"] == []


def finding(fid: str, violated: str, severity: str = "blocking") -> dict:
    return {
        "id": fid,
        "severity": severity,
        "description": "contradicts a decision",
        "proposal_id": "prop-a",
        "subject": "topology",
        "impact": "breaks the decision",
        "violated": violated,
        "evidence": ["the design replaces it"],
        "remediation": "keep the decision",
    }


def seal_reviews(d: Design, a_findings: list, s_findings: list | None = None) -> None:
    h = d.context_hash
    d.seal(
        "A2",
        {
            "critique_a_by_b_sealed": (
                "critique_a_by_b",
                critique("c-a", "prop-a", h, a_findings),
            ),
            "critique_b_by_a_sealed": ("critique_b_by_a", critique("c-b", "prop-b", h)),
        },
    )
    d.seal(
        "A3R",
        {
            "synthesis_review_sealed": (
                "synthesis_review",
                critique("c-s", "prop-s", h, s_findings),
            )
        },
    )


def seal_clean_design(d: Design) -> None:
    """Every proposal declares no dependencies, data flows or contract changes."""
    seal_with_contracts(d, [], [], [])


# ── H1 evidence ──────────────────────────────────────────────────────────────

EVIDENCE_KEYS = {
    "commit_sha",
    "context_manifest",
    "design_inputs",
    "vetoes",
    "scores",
    "survivors",
    "fired",
    "undetermined",
    "unevaluated",
}


def h1_evidence(design: Design) -> dict[str, Any]:
    policy = load_policy(design.rig.repo)
    return approval_evidence(design.rig.ctx(design.runner, "H1"), policy)


def test_h1_evidence_for_a_design_that_never_got_a_synthesis(design: Design) -> None:
    """A2 sends a spent budget to H1 before A3 runs: the human is still shown what exists."""
    design.seal_proposals()
    design.seal_critiques(
        a_on_b=critique("c1", "prop-b", design.context_hash, [blocking("f", "prop-b")])
    )
    evidence = h1_evidence(design)
    assert set(evidence) == EVIDENCE_KEYS | {"incomplete"}
    assert evidence["incomplete"] == ["synthesis_sealed"]
    assert evidence["context_manifest"] == design.context_hash
    # B1 has not run, so nothing is a survivor, and nothing is called clear or vetoed.
    assert evidence["survivors"] == [] and evidence["fired"] == {}
    assert evidence["vetoes"] is None and evidence["scores"] is None


def test_h1_evidence_is_bound_to_what_was_sealed(design: Design) -> None:
    design.seal_proposals()
    before = h1_evidence(design)
    assert h1_evidence(design) == before
    design.seal(
        "A1",
        {
            "proposal_a_sealed": ("proposal_a", proposal("prop-a2", design.context_hash)),
            "proposal_b_sealed": ("proposal_b", proposal("prop-b", design.context_hash)),
        },
    )
    assert h1_evidence(design)["design_inputs"] != before["design_inputs"]


def test_h1_evidence_for_a_complete_design_keeps_its_shape(design: Design) -> None:
    design.seal_valid()
    design.seal_reviews()
    evidence = h1_evidence(design)
    assert set(evidence) == EVIDENCE_KEYS
    assert (
        evidence["survivors"]
        == sorted(evidence["survivors"])
        == [
            "proposal_a_sealed",
            "proposal_b_sealed",
            "synthesis_sealed",
        ]
    )
    assert isinstance(evidence["vetoes"], str) and isinstance(evidence["scores"], str)


@pytest.mark.parametrize("complete", [False, True])
def test_h1_evidence_refuses_a_candidate_changed_after_sealing(
    design: Design, complete: bool
) -> None:
    """A missing candidate is reported; one that was produced and then altered is an error."""
    design.seal_valid() if complete else design.seal_proposals()
    root = design.rig.run_dir / "design" / "proposal_a"
    doc = json.loads((root / "proposal_a.json").read_text())
    (root / "proposal_a.json").write_text(json.dumps({**doc, "id": "forged"}))
    with pytest.raises(GateError, match="proposal_a_sealed"):
        h1_evidence(design)


def test_h1_evidence_still_needs_the_pinned_context(design: Design) -> None:
    design.seal_proposals()
    manifest = design.rig.run_dir / "manifests" / "G.json"
    manifest.unlink()
    with pytest.raises(GateError, match="pinned_context_manifest"):
        h1_evidence(design)


def test_the_gates_still_need_the_whole_design(design: Design) -> None:
    """Only the human's evidence tolerates a missing synthesis: B1 and A4 never do."""
    design.seal_proposals()
    for node in ("A4", "B1"):
        with pytest.raises(GateError, match="synthesis_sealed"):
            design.guard(node)
