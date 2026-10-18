"""A2J finding triage: the questions put to the decision model, and the verdicts from its answers.

Pure functions over sealed documents, used by both sides of the call: the decision adapter
builds the requests from its grant's inputs, and the A2J/A4/H1 gates rebuild the same requests
from sealed state, check that the logged prompt hash is theirs, and read the answers.

For every finding of both A2 critiques the judge gets one request: the target proposal and the
finding as compact JSON (its state), and one yes/no question, QUESTION. A finding is valid when
the probability of "yes" is at least the human-owned `valid_threshold` (policy/triage.yaml).
Nothing is deleted: every finding is reported with its probability, and the threshold was
calibrated for exactly this question, so changing the wording or the state layout voids it.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from agentic_pipeline.schemas import ValidationError, validate

QUESTION = "Is the finding substantiated?"
ANSWER = "valid"  # the question's name in every request
# critique operation -> (the sealed proposal it reviews, that proposal's operation)
CRITIQUES = {
    "critique_b_by_a": ("proposal_b_sealed", "proposal_b"),
    "critique_a_by_b": ("proposal_a_sealed", "proposal_a"),
}


class TriageError(Exception):
    pass


def _compact(document: Any) -> str:
    return json.dumps(document, separators=(",", ":"))


def request_id(critique: str, finding_id: str) -> str:
    """`<critique operation>_sealed/<finding id>`: the key A4's resolutions use, as one string."""
    return f"{critique}_sealed/{finding_id}"


def finding_requests(
    proposals: Mapping[str, Any], critiques: Mapping[str, Any]
) -> list[dict[str, Any]]:
    """One decision request per finding, critique by critique, in the critiques' order.

    `proposals` is keyed by sealed artifact (proposal_a_sealed, proposal_b_sealed) and
    `critiques` by critique operation; both must already be valid (A2 checked them).
    """
    requests: list[dict[str, Any]] = []
    for critique, (target, _) in CRITIQUES.items():
        proposal = _compact(proposals[target])
        for finding in critiques[critique]["findings"]:
            requests.append(
                {
                    "id": request_id(critique, finding["id"]),
                    "state": f"Proposal:\n{proposal}\n\nFinding:\n{_compact(finding)}",
                    "questions": {ANSWER: {"type": "noul", "instructions": QUESTION}},
                }
            )
    return requests


def decision_prompt(requests: list[dict[str, Any]]) -> bytes:
    """The bytes the adapter sends and hashes; the gates hash the same bytes to compare.

    Key order is kept, not sorted: it is the order the options are put to the model.
    """
    return json.dumps(requests, separators=(",", ":")).encode("utf-8")


def load_sealed(inputs: Mapping[str, Path]) -> tuple[dict[str, Any], dict[str, Any]]:
    """(proposals by sealed artifact, critiques by operation) from sealed input directories."""

    def read(artifact: str, operation: str) -> Any:
        return json.loads((inputs[artifact] / f"{operation}.json").read_text("utf-8"))

    proposals = {target: read(target, op) for target, op in CRITIQUES.values()}
    critiques = {critique: read(f"{critique}_sealed", critique) for critique in CRITIQUES}
    return proposals, critiques


def judge(critiques: Mapping[str, Any], result: Any, threshold: float) -> dict[str, Any]:
    """The triage report: every finding with its probability and verdict, ratios, waivers.

    `result` is the judge's decision_result document. It must answer every finding exactly
    once with one probability, and nothing else; anything less raises, so a finding is never
    waived for want of an answer.
    """
    try:
        validate("decision_result", result)
    except ValidationError as exc:
        raise TriageError(f"verdicts: schema: {exc.message}") from exc
    answered: dict[str, float] = {}
    for item in result["results"]:
        if item["id"] in answered:
            raise TriageError(f"verdicts: {item['id']} answered twice")
        answer = item["answers"]
        if set(answer) != {ANSWER} or answer[ANSWER]["type"] != "noul":
            raise TriageError(f"verdicts: {item['id']} is not one yes/no answer")
        answered[item["id"]] = float(answer[ANSWER]["noul"])
    findings: list[dict[str, Any]] = []
    ratios: dict[str, float | None] = {}
    for critique in CRITIQUES:
        mine = []
        for finding in critiques[critique]["findings"]:
            rid = request_id(critique, finding["id"])
            if rid not in answered:
                raise TriageError(f"verdicts: {rid} has no answer")
            probability = answered.pop(rid)
            mine.append(
                {
                    "critique": f"{critique}_sealed",
                    "finding_id": finding["id"],
                    "severity": finding["severity"],
                    "probability": probability,
                    "valid": probability >= threshold,
                }
            )
        ratios[f"{critique}_sealed"] = sum(f["valid"] for f in mine) / len(mine) if mine else None
        findings.extend(mine)
    if answered:
        raise TriageError(f"verdicts: answers for findings never raised: {sorted(answered)}")
    return {
        "question": QUESTION,
        "threshold": threshold,
        "model": result["model"],
        "model_version": result["model_version"],
        "findings": findings,
        "valid_ratios": ratios,
        "waived_blocking": [f for f in findings if f["severity"] == "blocking" and not f["valid"]],
    }
