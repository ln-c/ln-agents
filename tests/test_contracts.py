"""Contract-break verdicts from a proposal's declared contract changes."""

from __future__ import annotations

import pytest

from agentic_pipeline.contracts import candidate_verdict, touch_verdict

PINNED = {"contracts/api.yaml": "abc123"}


def touch(path: str, change: object) -> dict:
    return {"path": path, "change": change}


@pytest.mark.parametrize(
    ("change", "expected"),
    [("breaking", True), ("compatible", False), ("none", False), ("other", None)],
)
def test_touch_verdict_for_a_pinned_contract(change: str, expected: bool | None) -> None:
    assert touch_verdict(touch("contracts/api.yaml", change), PINNED)[0] is expected


def test_a_contract_that_is_not_pinned_cannot_be_judged() -> None:
    verdict, reason = touch_verdict(touch("contracts/ghost.yaml", "breaking"), PINNED)
    assert verdict is None and "not a contract pinned" in reason


def test_absent_list_is_undetermined_and_empty_list_is_clear() -> None:
    assert candidate_verdict({}, PINNED) is None
    assert candidate_verdict({"contracts_touched": []}, PINNED) is False
    assert candidate_verdict({"contracts_touched": []}, {}) is False


def test_a_break_fires_even_beside_an_undecided_touch() -> None:
    touched = [touch("contracts/ghost.yaml", "none"), touch("contracts/api.yaml", "breaking")]
    assert candidate_verdict({"contracts_touched": touched}, PINNED) is True


def test_one_undecided_touch_keeps_the_rest_from_clearing() -> None:
    touched = [touch("contracts/api.yaml", "none"), touch("contracts/ghost.yaml", "none")]
    assert candidate_verdict({"contracts_touched": touched}, PINNED) is None
