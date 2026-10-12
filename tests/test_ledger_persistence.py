"""Tests for the budget ledger's persistence hook (fail-closed semantics)."""

from __future__ import annotations

from pathlib import Path

import pytest

from agentic_pipeline.graph import Edge, Graph
from agentic_pipeline.transition import BudgetLedger, BudgetUnavailable

ROOT = Path(__file__).resolve().parents[1]
EDGE = Edge("A2", "A1", "blocking_flaw", "design")


class Recorder:
    def __init__(self) -> None:
        self.snapshots: list[dict[str, dict[str, int]]] = []
        self.fail = False

    def __call__(self, ledger: BudgetLedger) -> None:
        if self.fail:
            raise OSError("disk full")
        self.snapshots.append(ledger.snapshot())


def ledger(recorder: Recorder, limit: int = 2) -> BudgetLedger:
    return BudgetLedger({"design": limit}, listener=recorder)


def test_every_change_is_persisted_in_order() -> None:
    rec = Recorder()
    first = ledger(rec).reserve(EDGE)
    first.commit()
    assert rec.snapshots == [
        {"consumed": {}, "reserved": {"design": 1}},
        {"consumed": {"design": 1}, "reserved": {}},
    ]


def test_release_is_persisted() -> None:
    rec = Recorder()
    ledger(rec).reserve(EDGE).release()
    assert rec.snapshots[-1] == {"consumed": {}, "reserved": {}}


def test_failed_reserve_write_spends_nothing() -> None:
    rec = Recorder()
    rec.fail = True
    led = ledger(rec, limit=1)
    with pytest.raises(BudgetUnavailable, match="cannot persist"):
        led.reserve(EDGE)
    assert led.snapshot() == {"consumed": {}, "reserved": {}}
    rec.fail = False
    assert led.reserve(EDGE) is not None  # the unit was not lost


def test_failed_commit_write_propagates_but_stays_spent() -> None:
    rec = Recorder()
    led = ledger(rec)
    reservation = led.reserve(EDGE)
    rec.fail = True
    with pytest.raises(OSError, match="disk full"):
        reservation.commit()
    assert led.consumed == {"design": 1}  # memory says spent; disk still says reserved


def test_failed_release_write_is_swallowed_and_corrected_by_next_write() -> None:
    rec = Recorder()
    led = ledger(rec)
    reservation = led.reserve(EDGE)
    rec.fail = True
    reservation.release()  # must not raise inside error handlers
    assert led.reserved == {"design": 0}
    rec.fail = False
    led.reserve(EDGE)
    assert rec.snapshots[-1] == {"consumed": {}, "reserved": {"design": 1}}  # full rewrite


def test_snapshot_omits_zero_counts() -> None:
    led = BudgetLedger({"design": 2, "regen": 2}, consumed={"design": 1, "regen": 0})
    assert led.snapshot() == {"consumed": {"design": 1}, "reserved": {}}


def test_ledger_without_listener_behaves_as_before() -> None:
    led = BudgetLedger.from_policy({"budgets": {"design": {"max_retries": 1}}})
    led.reserve(EDGE).commit()
    assert Graph.load(ROOT / "pipeline" / "graph.yaml") and led.consumed == {"design": 1}
