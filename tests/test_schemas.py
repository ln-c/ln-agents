"""Tests for artifact schemas using fastjsonschema."""

from __future__ import annotations

import pytest

from agentic_pipeline.schemas import ValidationError, schema_names, validate


def test_schema_names_discovery() -> None:
    names = schema_names()
    assert "context_manifest" in names
    assert "proposal" in names
    assert "scores" in names
    assert "decision_record" in names
    assert "adr" in names
    assert "exception_payload" in names
    assert "provenance_event" in names


def test_decision_record_rejects_human_approver() -> None:
    valid_dr = {
        "id": "DR-001",
        "run_id": "run-123",
        "winner": "candidate-A",
        "runner_up": "candidate-B",
        "policy_version": "v1.0",
        "auto_generated": True,
        "rationale": "Clear winner on security",
    }
    validate("decision_record", valid_dr)

    # Reject if approver_id is present
    with pytest.raises(ValidationError):
        validate("decision_record", {**valid_dr, "approver_id": "human-alice"})

    # Reject if approved_by is present
    with pytest.raises(ValidationError):
        validate("decision_record", {**valid_dr, "approved_by": "human-alice"})

    # Reject if status=accepted
    with pytest.raises(ValidationError):
        validate("decision_record", {**valid_dr, "status": "accepted"})


def test_adr_requires_human_approver() -> None:
    valid_adr = {
        "id": "0003",
        "title": "Use SQLite for Local Storage",
        "status": "accepted",
        "approver_id": "human-alice",
        "reason": "Lightweight embedded DB meets local latency budget",
        "run_id": "run-123",
        "approved_at": "2026-10-11T12:00:00Z",
    }
    validate("adr", valid_adr)

    # Missing approver_id should fail
    bad = dict(valid_adr)
    bad.pop("approver_id")
    with pytest.raises(ValidationError):
        validate("adr", bad)


def test_proposal_synthesized_requires_sources() -> None:
    raw_proposal = {
        "id": "prop-1",
        "author_model": "gemini-3.1-pro",
        "synthesized": False,
        "topology": {"components": [{"name": "api-gateway"}]},
        "manifest_hash": "sha256:abc",
    }
    validate("proposal", raw_proposal)

    # Synthesized without sources fails
    synthesized_missing_sources = {
        **raw_proposal,
        "id": "prop-synth",
        "synthesized": True,
    }
    with pytest.raises(ValidationError):
        validate("proposal", synthesized_missing_sources)

    # Synthesized with sources passes
    synthesized_valid = {
        **synthesized_missing_sources,
        "sources": {"api-gateway": "prop-1"},
    }
    validate("proposal", synthesized_valid)


def test_scores_type_discrimination() -> None:
    valid_scores = {
        "weights_version": "1.0",
        "values": [
            {
                "candidate": "prop-1",
                "criterion": "security",
                "type": "criterion_score",
                "value": 3,
            },
            {
                "candidate": "prop-1",
                "criterion": "scalability",
                "type": "outcome_probability",
                "value": 0.85,
                "task_class": "backend_api",
                "calibration_label": "clef_2026q1",
            },
            {
                "candidate": "prop-1",
                "criterion": "cost",
                "type": "uncertainty",
                "value": 0.12,
            },
        ],
        "flags": {
            "near_tie": False,
            "high_uncertainty": False,
        },
    }
    validate("scores", valid_scores)

    # outcome_probability missing task_class or calibration_label must fail
    bad_prob = {
        "weights_version": "1.0",
        "values": [
            {
                "candidate": "prop-1",
                "criterion": "scalability",
                "type": "outcome_probability",
                "value": 0.85,
            }
        ],
        "flags": {"near_tie": False, "high_uncertainty": False},
    }
    with pytest.raises(ValidationError):
        validate("scores", bad_prob)
