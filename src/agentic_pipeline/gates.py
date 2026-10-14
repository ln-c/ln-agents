"""Deterministic design-side gates: A2, A4, B1, B2, B3, and DR.

Each handler recomputes its facts from sealed artifacts and frozen policy, picks a guard,
and writes an audit record under `gates/<NODE>/<input digest>.json`. Naming the record by
the digest of the inputs makes a handler a pure function of them: a gate that re-runs after
an A1 or A3 retry sees new inputs and writes a new record, and never returns a stale guard.

Authoritative proposal format: `schemas/proposal.schema.json`. A design stage writes its
proposal as `<operation_id>.json` in its write root (e.g. `proposal_a.json`).

Critique format: `schemas/critique.schema.json`; a critic writes `<operation_id>.json`
(e.g. `critique_b_by_a.json`) in its write root.

Rules these gates keep:
* A missing, tampered, or unreadable sealed artifact raises (BLOCKED); only a proposal's
  *content* can make it invalid.
* A4 never returns `design_spent` (the runner substitutes the spent route itself) or
  `constraint_sources_conflict` (no deterministic definition exists; reported as not
  evaluated).
* A veto that cannot be evaluated is never treated as cleared: B1 lists it, and B3's
  `no_active_veto` fails while any veto is unevaluated, or undetermined for a candidate (for
  example a proposal that does not declare its dependencies, or a dependency the pinned
  advisory snapshot does not cover).
* B2 scores each surviving candidate from an independent review: A from `critique_a_by_b`,
  B from `critique_b_by_a`, S from A3R's `synthesis_review`. A reviewer's 0-4 scores are used
  as given (the scores schema's scale); a missing score, an invalid or unbound review, or a
  reviewer on the author's backend leaves the criterion unscored, and unscored criteria raise
  uncertainty. One scorer per candidate means no disagreement signal; a blocking finding
  against S (which no gate routes) marks it contested and sets `high_uncertainty`.
* B3 fails every `require_all` item that has no evaluator.
* DR refuses to run: it needs a scored winner and none can exist yet.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from agentic_pipeline.advisories import candidate_verdict as dependency_verdict
from agentic_pipeline.contracts import candidate_verdict as contract_verdict
from agentic_pipeline.dataflow import candidate_verdict as flow_verdict
from agentic_pipeline.frozen import PolicyChanged, frozen_policy
from agentic_pipeline.fsutil import atomic_write_json
from agentic_pipeline.manifest import (
    ManifestError,
    build_manifest,
    digest_path,
    load_manifest,
    publish_stage_manifest,
    resolve_sealed_inputs,
)
from agentic_pipeline.run_state import PolicySnapshot
from agentic_pipeline.schemas import ValidationError, validate

if TYPE_CHECKING:
    from agentic_pipeline.runner import HandlerContext

GATES_DIR = "gates"
DESIGN_INPUTS = ("proposal_a_sealed", "proposal_b_sealed", "synthesis_sealed")
CONTEXT_INPUT = "pinned_context_manifest"
UNEVALUATED = "not_evaluated"


class GateError(Exception):
    pass


# ── inputs ───────────────────────────────────────────────────────────────────


def _policy(ctx: HandlerContext) -> PolicySnapshot:
    try:
        return frozen_policy(ctx)
    except PolicyChanged as exc:
        raise GateError(str(exc)) from exc


def _sealed(ctx: HandlerContext, ids: list[str]) -> dict[str, Path]:
    inputs, errors = resolve_sealed_inputs(ctx.run_root, ctx.run_id, ctx.registry, ids)
    if errors:
        raise GateError("; ".join(errors))
    return {name: item.path for name, item in inputs.items()}


def _canonical(document: Any) -> bytes:
    return json.dumps(document, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()


def _digest(document: Any) -> str:
    return hashlib.sha256(_canonical(document)).hexdigest()


def _record(ctx: HandlerContext, node: str, key: str, document: Mapping[str, Any]) -> Path:
    path = ctx.run_root / GATES_DIR / node / f"{key}.json"
    atomic_write_json(path, document)
    return path


class Design:
    """The sealed design artifacts as parsed JSON, plus what could not be parsed."""

    def __init__(self, ctx: HandlerContext) -> None:
        paths = _sealed(ctx, [CONTEXT_INPUT, *DESIGN_INPUTS])
        context = json.loads((paths[CONTEXT_INPUT] / "context_manifest.json").read_text("utf-8"))
        self.context_hash: str = context["hash"]
        self.contract_versions: dict[str, str] = context["contract_versions"]
        self.input_digest = _digest({name: digest_path(path) for name, path in paths.items()})
        self.docs: dict[str, Any] = {}
        self.parse_errors: dict[str, str] = {}
        operations = {
            "proposal_a_sealed": "proposal_a",
            "proposal_b_sealed": "proposal_b",
            "synthesis_sealed": "synthesis",
        }
        for artifact, operation in operations.items():
            file = paths[artifact] / f"{operation}.json"
            try:
                self.docs[artifact] = json.loads(file.read_text("utf-8"))
            except (OSError, ValueError) as exc:
                self.parse_errors[artifact] = f"{operation}.json: {exc}"


def _schema_errors(doc: Any) -> list[str]:
    try:
        validate("proposal", doc)
    except ValidationError as exc:
        return [f"schema: {exc.message}"]
    return []


def design_problems(design: Design) -> dict[str, list[str]]:
    """Per sealed artifact, why it is invalid (empty list = valid)."""
    problems: dict[str, list[str]] = {}
    for artifact in DESIGN_INPUTS:
        if artifact in design.parse_errors:
            problems[artifact] = [design.parse_errors[artifact]]
            continue
        doc = design.docs[artifact]
        errors = _schema_errors(doc)
        if not errors:
            expect_synth = artifact == "synthesis_sealed"
            if doc["synthesized"] is not expect_synth:
                errors.append(f"synthesized must be {str(expect_synth).lower()}")
            if doc["manifest_hash"] != design.context_hash:
                errors.append("manifest_hash does not match the pinned context manifest")
        problems[artifact] = errors
    ids = {a: design.docs[a].get("id") for a in DESIGN_INPUTS if not problems[a]}
    if len(set(ids.values())) != len(ids):
        for artifact in ids:
            problems[artifact].append("proposal ids must be distinct")
    if not problems["synthesis_sealed"] and not (
        problems["proposal_a_sealed"] or problems["proposal_b_sealed"]
    ):
        allowed = {ids["proposal_a_sealed"], ids["proposal_b_sealed"]}
        sources = set(design.docs["synthesis_sealed"].get("sources", {}).values())
        if not sources <= allowed:
            problems["synthesis_sealed"].append(f"sources must name only {sorted(allowed)}")
    return problems


# ── A4 ───────────────────────────────────────────────────────────────────────


def validate_design(ctx: HandlerContext) -> str:
    """A4: schema, `manifest_hash`, and traceability checks on proposals A, B and S."""
    _policy(ctx)
    design = Design(ctx)
    problems = design_problems(design)
    if problems["proposal_a_sealed"] or problems["proposal_b_sealed"]:
        guard = "proposal_invalid"
    elif problems["synthesis_sealed"]:
        guard = "synthesis_invalid"
    else:
        guard = "all_required_valid"
    _record(
        ctx,
        "A4",
        design.input_digest,
        {"guard": guard, "problems": problems, "constraint_sources_conflict": UNEVALUATED},
    )
    return guard


# ── A2 ───────────────────────────────────────────────────────────────────────

# critique operation -> (the proposal it reviews, that proposal's producing operation)
CRITIQUES = {
    "critique_b_by_a": ("proposal_b_sealed", "proposal_b"),
    "critique_a_by_b": ("proposal_a_sealed", "proposal_a"),
}


def _load_json(directory: Path, name: str) -> Any:
    try:
        return json.loads((directory / f"{name}.json").read_text("utf-8"))
    except (OSError, ValueError) as exc:
        raise GateError(f"{name}.json: {exc}") from exc


def _backend(ctx: HandlerContext, stage: str, operation: str) -> str:
    try:
        doc = load_manifest(ctx.run_root, stage, ctx.run_id)
    except ManifestError as exc:
        raise GateError(f"{stage}: {exc}") from exc
    if doc is None or operation not in doc["operations"]:
        raise GateError(f"{stage}.{operation}: no recorded backend")
    return str(doc["operations"][operation]["backend"])


def critique_problems(ctx: HandlerContext) -> tuple[str, dict[str, Any], dict[str, list[str]]]:
    """(input digest, parsed critiques, problems per critique).

    A critique with any problem is not evidence of a review, so the caller blocks rather
    than reading it as "no flaws". Finding counts are recomputed from the findings.
    """
    names = [
        CONTEXT_INPUT,
        *(t for t, _ in CRITIQUES.values()),
        *(f"{c}_sealed" for c in CRITIQUES),
    ]
    paths = _sealed(ctx, names)
    context = _load_json(paths[CONTEXT_INPUT], "context_manifest")
    digest = _digest({name: digest_path(path) for name, path in paths.items()})
    docs: dict[str, Any] = {}
    problems: dict[str, list[str]] = {}
    for operation, (target, target_op) in CRITIQUES.items():
        doc = _load_json(paths[f"{operation}_sealed"], operation)
        docs[operation] = doc
        found = _critique_errors(doc)
        if not found:
            target_doc = _load_json(paths[target], target_op)
            found = _critique_binding_errors(ctx, operation, target_op, doc, target_doc, context)
        problems[operation] = found
    return digest, docs, problems


def _critique_errors(doc: Any) -> list[str]:
    try:
        validate("critique", doc)
    except ValidationError as exc:
        return [f"schema: {exc.message}"]
    errors = []
    derived = {"blocking": 0, "non_blocking": 0, "informational": 0}
    for finding in doc["findings"]:
        derived[finding["severity"]] += 1
    if derived != doc["counts"]:
        errors.append(f"counts {doc['counts']} do not match the findings {derived}")
    if not doc["review_complete"]:
        errors.append("review_complete is false")
    return errors


def _critique_binding_errors(
    ctx: HandlerContext,
    operation: str,
    target_op: str,
    doc: Mapping[str, Any],
    target: Mapping[str, Any],
    context: Mapping[str, Any],
) -> list[str]:
    errors = []
    if doc["manifest_hash"] != context["hash"]:
        errors.append("manifest_hash does not match the pinned context manifest")
    if doc["target_proposal_id"] != target.get("id"):
        errors.append(f"target_proposal_id is not the {target_op} id {target.get('id')!r}")
    for finding in doc["findings"]:
        if finding.get("proposal_id", doc["target_proposal_id"]) != doc["target_proposal_id"]:
            errors.append(f"finding {finding['id']} is about a different proposal")
    # Independence comes from the recorded backends, not from a model's own claim.
    if _backend(ctx, "A2", operation) == _backend(ctx, "A1", target_op):
        errors.append("critic ran on the same backend as the proposal it reviews")
    return errors


def assess_critiques(ctx: HandlerContext) -> str:
    """A2: any blocking finding means `blocking_flaw`; otherwise `no_blocking_flaw`.

    An invalid, incomplete, or unbound critique blocks the step: it is not a finding, and
    it must not read as a clean review. `design_spent` is never chosen here; the runner
    substitutes the spent route when the budgeted `blocking_flaw` edge is exhausted.
    """
    _policy(ctx)
    digest, docs, problems = critique_problems(ctx)
    bad = {op: errs for op, errs in problems.items() if errs}
    if bad:
        raise GateError(f"critique not usable: {bad}")
    blocking = {op: doc["counts"]["blocking"] for op, doc in docs.items()}
    guard = "blocking_flaw" if any(blocking.values()) else "no_blocking_flaw"
    _record(ctx, "A2", digest, {"guard": guard, "blocking_findings": blocking})
    return guard


# ── B1 ───────────────────────────────────────────────────────────────────────


# veto id -> does this candidate violate it? True fires the veto, False clears it, None means
# the proposal or the policy snapshot cannot say (reported as undetermined, never as clear).
# Only checks decidable from a proposal document belong here; every other veto in
# policy/vetoes.yaml is reported as unevaluated.
@dataclass(frozen=True, slots=True)
class VetoFacts:
    """What an evaluator may consult besides the proposal: frozen policy and the entry spec."""

    policy: PolicySnapshot
    entry_data: Mapping[str, Any]
    contracts: Mapping[str, str]


type VetoEvaluator = Callable[[Any, VetoFacts], bool | None]
VETO_EVALUATORS: dict[str, VetoEvaluator] = {
    "schema_invalid": lambda doc, facts: bool(_schema_errors(doc)),
    "known_vulnerable_dependency": lambda doc, facts: (
        dependency_verdict(doc, facts.policy.advisories) if not _schema_errors(doc) else None
    ),
    "contract_break": lambda doc, facts: (
        contract_verdict(doc, facts.contracts) if not _schema_errors(doc) else None
    ),
    "data_class_violation": lambda doc, facts: (
        flow_verdict(doc, facts.policy.data_classes, facts.entry_data)
        if not _schema_errors(doc)
        else None
    ),
}


class VetoReport:
    def __init__(self, ctx: HandlerContext) -> None:
        policy = _policy(ctx)
        design = Design(ctx)
        known = {v["id"] for v in policy.vetoes["vetoes"]}
        stray = sorted(set(VETO_EVALUATORS) - known)
        if stray:
            raise GateError(f"evaluators for vetoes not in policy/vetoes.yaml: {stray}")
        entry = json.loads(
            (_sealed(ctx, ["requirements"])["requirements"] / "entry_spec.json").read_text("utf-8")
        )
        facts = VetoFacts(policy, entry["data"], design.contract_versions)
        self.input_digest = design.input_digest
        self.evaluated = sorted(set(VETO_EVALUATORS))
        self.unevaluated = sorted(known - set(VETO_EVALUATORS))
        self.fired: dict[str, list[str]] = {}
        self.undetermined: dict[str, list[str]] = {}
        self.survivors: list[str] = []
        for artifact in DESIGN_INPUTS:
            doc = design.docs.get(artifact)
            fired: list[str] = []
            undetermined: list[str] = []
            if doc is None:
                fired = ["schema_invalid"]
            else:
                for veto, check in VETO_EVALUATORS.items():
                    verdict = check(doc, facts)
                    if verdict is None:
                        undetermined.append(veto)
                    elif verdict:
                        fired.append(veto)
            self.fired[artifact] = fired
            self.undetermined[artifact] = undetermined
            if not fired:
                self.survivors.append(artifact)

    @property
    def clear(self) -> bool:
        """True only if no veto fired and none was left unevaluated or undetermined."""
        return (
            not any(self.fired.values())
            and not any(self.undetermined.values())
            and not self.unevaluated
        )

    def document(self, guard: str) -> dict[str, Any]:
        return {
            "guard": guard,
            "evaluated": self.evaluated,
            "unevaluated": self.unevaluated,
            "undetermined": self.undetermined,
            "fired": self.fired,
            "survivors": self.survivors,
        }


def apply_vetoes(ctx: HandlerContext) -> str:
    """B1: hard vetoes. A candidate that cannot be shown clean is not called clean."""
    report = VetoReport(ctx)
    # No veto carries a "correctable" flag in policy, so a fired veto is never correctable.
    guard = "at_least_one_survivor" if report.survivors else "all_vetoed_otherwise"
    _record(ctx, "B1", report.input_digest, report.document(guard))
    return guard


# ── B2 ───────────────────────────────────────────────────────────────────────

# candidate artifact -> (the critique that scores it, its sealed artifact, the stage and
# operation that wrote the scored proposal, the stage and operation that wrote the critique)
SCORERS = {
    "proposal_a_sealed": ("critique_a_by_b", "critique_a_by_b_sealed", ("A1", "proposal_a")),
    "proposal_b_sealed": ("critique_b_by_a", "critique_b_by_a_sealed", ("A1", "proposal_b")),
    "synthesis_sealed": ("synthesis_review", "synthesis_review_sealed", ("A3", "synthesis")),
}
CRITIC_STAGES = {"critique_a_by_b": "A2", "critique_b_by_a": "A2", "synthesis_review": "A3R"}


def _scorer_inputs(
    ctx: HandlerContext, survivors: list[str]
) -> tuple[dict[str, Path], dict[str, str]]:
    """Sealed reviews of the survivors; one never published is a reason, not an error."""
    paths: dict[str, Path] = {}
    missing: dict[str, str] = {}
    for candidate in survivors:
        _, artifact, _ = SCORERS[candidate]
        found, errors = resolve_sealed_inputs(ctx.run_root, ctx.run_id, ctx.registry, [artifact])
        if errors and all("no published manifest" in e for e in errors):
            missing[candidate] = "no review was published for this candidate"
        elif errors:
            raise GateError("; ".join(errors))
        else:
            paths[candidate] = found[artifact].path
    return paths, missing


def _score_one(
    ctx: HandlerContext, candidate: str, path: Path, proposal: Any, context_hash: str
) -> tuple[dict[str, int], list[str], bool]:
    """(criterion -> 0..4, why not every criterion scored, contested) for one candidate."""
    operation, _, (target_stage, target_op) = SCORERS[candidate]
    critique = _load_json(path, operation)
    problems = _critique_errors(critique)
    if not problems:
        if critique["manifest_hash"] != context_hash:
            problems.append("manifest_hash does not match the pinned context manifest")
        if critique["target_proposal_id"] != (proposal or {}).get("id"):
            problems.append("target_proposal_id is not the candidate's id")
        if _backend(ctx, CRITIC_STAGES[operation], operation) == _backend(
            ctx, target_stage, target_op
        ):
            problems.append("the reviewer ran on the same backend as the author")
    if problems:
        return {}, problems, False
    given = critique.get("scores", {})
    values = {c: v["score"] for c, v in given.items()}
    return values, [], critique["counts"]["blocking"] > 0


def build_scores(ctx: HandlerContext, policy: PolicySnapshot, report: VetoReport) -> dict[str, Any]:
    """The B2 scores record: a pure function of the sealed design, reviews, and frozen policy."""
    weights: Mapping[str, float] = policy.weights["weights"]
    thresholds = policy.weights["thresholds"]
    design = Design(ctx)
    paths, missing = _scorer_inputs(ctx, report.survivors)
    values: list[dict[str, Any]] = []
    candidates: dict[str, Any] = {}
    for candidate in report.survivors:
        scored: dict[str, int] = {}
        reasons: list[str] = []
        contested = False
        if candidate in missing:
            reasons.append(missing[candidate])
        else:
            scored, reasons, contested = _score_one(
                ctx, candidate, paths[candidate], design.docs.get(candidate), design.context_hash
            )
        unscored = sorted(set(weights) - set(scored))
        uncertainty = len(unscored) / len(weights)
        weighted = None if unscored else sum(w * scored[c] / 4 for c, w in weights.items())
        for criterion in sorted(scored.keys() & set(weights)):
            values.append(
                {
                    "candidate": candidate,
                    "criterion": criterion,
                    "type": "criterion_score",
                    "value": scored[criterion],
                }
            )
        values.append(
            {
                "candidate": candidate,
                "criterion": "all",
                "type": "uncertainty",
                "value": uncertainty,
            }
        )
        candidates[candidate] = {
            "weighted": weighted,
            "unscored_criteria": unscored,
            "contested": contested,
            "reasons": reasons,
        }
    ranked = sorted(
        (c for c, d in candidates.items() if d["weighted"] is not None),
        key=lambda c: -candidates[c]["weighted"],
    )
    gap_small = len(ranked) >= 2 and (
        candidates[ranked[0]]["weighted"] - candidates[ranked[1]]["weighted"]
        < thresholds["near_tie_delta"]
    )
    # Fewer than two comparable candidates cannot show a clear winner among several survivors.
    uncomparable = len(report.survivors) >= 2 and len(ranked) < 2
    uncertainties = [v["value"] for v in values if v["type"] == "uncertainty"]
    mean = sum(uncertainties) / len(uncertainties) if uncertainties else 1.0
    return {
        "weights_version": str(policy.weights["version"]),
        "values": values,
        "flags": {
            "near_tie": gap_small or uncomparable,
            "high_uncertainty": mean > thresholds["high_uncertainty"]
            or any(d["contested"] for d in candidates.values())
            or not candidates,
        },
        "scored": bool(ranked),
        "ranking": ranked,
        "candidates": candidates,
    }


def _scores_key(ctx: HandlerContext, report: VetoReport) -> str:
    keys = {"design": report.input_digest}
    for candidate in report.survivors:
        _, artifact, _ = SCORERS[candidate]
        found, _ = resolve_sealed_inputs(ctx.run_root, ctx.run_id, ctx.registry, [artifact])
        if artifact in found:
            keys[artifact] = digest_path(found[artifact].path)
    return _digest(keys)


def score_candidates(ctx: HandlerContext) -> str:
    """B2: criterion scores from the independent reviews; unscored where there is no evidence."""
    policy = _policy(ctx)
    report = VetoReport(ctx)
    record = build_scores(ctx, policy, report)
    try:
        validate("scores", record)
    except ValidationError as exc:
        raise GateError(f"scores record invalid: {exc.message}") from exc
    key = _scores_key(ctx, report)
    out = ctx.run_root / GATES_DIR / "B2" / key / "scores"
    out.mkdir(parents=True, exist_ok=True)
    atomic_write_json(out / "scores.json", record)
    try:
        manifest = build_manifest(
            ctx.run_root,
            run_id=ctx.run_id,
            stage="B2",
            attempt=key[:32],
            artifacts={"scores": (out, True)},
            operations={},
            provenance_tip=ctx.provenance_tip(),
            data_classes={"scores": _most_restrictive(ctx, policy)},
        )
        publish_stage_manifest(ctx.run_root, manifest)
    except ManifestError as exc:
        raise GateError(f"B2: manifest not published: {exc}") from exc
    return "always"


def _most_restrictive(ctx: HandlerContext, policy: PolicySnapshot) -> str:
    classes = [
        item.data_class
        for item in resolve_sealed_inputs(
            ctx.run_root, ctx.run_id, ctx.registry, list(DESIGN_INPUTS)
        )[0].values()
    ]
    order = policy.data_classes["order"]
    return max(classes or ["unknown"], key=lambda c: order.index(c) if c in order else len(order))


# ── B3 ───────────────────────────────────────────────────────────────────────

# require_all item -> True (holds), False (does not hold), or None (cannot be evaluated).
# An item with no evaluator, or an evaluator that returns None, fails closed.
type Evaluator = Callable[["B3Facts"], bool | None]


class B3Facts:
    def __init__(self, ctx: HandlerContext) -> None:
        self.policy = _policy(ctx)
        self.vetoes = VetoReport(ctx)
        self.scores = build_scores(ctx, self.policy, self.vetoes)
        paths = _sealed(ctx, ["requirements"])
        spec = json.loads((paths["requirements"] / "entry_spec.json").read_text("utf-8"))
        self.task_class = spec["classification"]["primary_class"]
        inputs, errors = resolve_sealed_inputs(
            ctx.run_root, ctx.run_id, ctx.registry, ["applicable_contracts", "protected_tests"]
        )
        self.grounding_intact = not errors and len(inputs) == 2


EVALUATORS: dict[str, Evaluator] = {
    "task_class_on_allowlist": lambda f: (
        f.task_class in (f.policy.fast_path.get("task_class_allowlist") or [])
    ),
    "no_active_veto": lambda f: f.vetoes.clear,
    "contracts_and_protected_tests_intact": lambda f: f.grounding_intact,
    "no_near_tie": lambda f: None if not f.scores["scored"] else not f.scores["flags"]["near_tie"],
    "no_high_uncertainty": lambda f: not f.scores["flags"]["high_uncertainty"],
}


def authorize(ctx: HandlerContext) -> str:
    """B3: fast path only if every `require_all` item is evaluated and holds."""
    facts = B3Facts(ctx)
    results: dict[str, str] = {}
    for item in facts.policy.fast_path["require_all"]:
        evaluator = EVALUATORS.get(item)
        outcome = evaluator(facts) if evaluator else None
        results[item] = UNEVALUATED if outcome is None else ("passed" if outcome else "failed")
    guard = "fast_path_eligible" if all(r == "passed" for r in results.values()) else "otherwise"
    _record(ctx, "B3", facts.vetoes.input_digest, {"guard": guard, "require_all": results})
    return guard


# ── DR ───────────────────────────────────────────────────────────────────────


def record_decision(ctx: HandlerContext) -> str:
    raise GateError("DR needs a scored winner; none exists, and none is ever invented")


GATE_HANDLERS: dict[str, Callable[[Any], str]] = {
    "A2": assess_critiques,
    "A4": validate_design,
    "B1": apply_vetoes,
    "B2": score_candidates,
    "B3": authorize,
    "DR": record_decision,
}
