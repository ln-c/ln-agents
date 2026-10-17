"""Tests for the hash-chained L1 provenance log."""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

import pytest

from agentic_pipeline.provenance import (
    GENESIS_HASH,
    JsonlProvenanceLog,
    ProvenanceError,
    canonical_hash,
    log_path,
    verify_provenance_chain,
)


def call(invocation: str, op: str = "proposal_a") -> dict[str, Any]:
    return {
        "state": "A1",
        "event": "model_call",
        "model": "claude",
        "model_version": "2.1.296",
        "prompt_hash": "sha256:" + "a" * 64,
        "backend": "claude_cli_default",
        "access_method": "subscription_tool",
        "operation_id": op,
        "invocation_id": invocation,
    }


def result(invocation: str, op: str = "proposal_a") -> dict[str, Any]:
    return {
        "state": "A1",
        "event": "model_result",
        "reason_code": "ok",
        "backend": "claude_cli_default",
        "access_method": "subscription_tool",
        "operation_id": op,
        "invocation_id": invocation,
    }


@pytest.fixture
def log(tmp_path: Path) -> JsonlProvenanceLog:
    return JsonlProvenanceLog(tmp_path, "run-1")


def _records(log: JsonlProvenanceLog) -> list[dict[str, Any]]:
    return [json.loads(line) for line in log.path.read_text().splitlines()]


def _rewrite(log: JsonlProvenanceLog, records: list[dict[str, Any]]) -> None:
    log.path.write_text("".join(json.dumps(r) + "\n" for r in records))


def test_chain_links_and_verifies(log: JsonlProvenanceLog) -> None:
    first = log.append({"state": "A1", "event": "enter"})
    second = log.append(call("i1"))
    log.append(result("i1"))
    assert first["seq"] == 0 and first["prev_hash"] == GENESIS_HASH
    assert second["prev_hash"] == first["event_hash"]
    assert second["event_hash"] == canonical_hash(second)
    report = verify_provenance_chain(log.path)
    assert report.ok and report.count == 3, report


def test_log_rejects_caller_assigned_fields(log: JsonlProvenanceLog) -> None:
    with pytest.raises(ValueError, match="assigned by the log"):
        log.append({"state": "A1", "event": "enter", "seq": 7})


def test_schema_invalid_event_is_not_written(log: JsonlProvenanceLog) -> None:
    incomplete = call("i1")
    del incomplete["invocation_id"]
    with pytest.raises(ProvenanceError):
        log.append(incomplete)
    assert not log.path.exists() or log.path.read_text() == ""


@pytest.mark.parametrize("run_id", ["../escape", "a/b", "", "x" * 65, "run 1"])
def test_run_id_rejected(tmp_path: Path, run_id: str) -> None:
    with pytest.raises(ValueError):
        log_path(tmp_path, run_id)


def test_modified_record_detected(log: JsonlProvenanceLog) -> None:
    log.append(call("i1"))
    log.append(result("i1"))
    records = _records(log)
    records[0]["model_version"] = "9.9.9"
    _rewrite(log, records)
    assert any("event_hash mismatch" in e for e in verify_provenance_chain(log.path).errors)


def test_reordered_records_detected(log: JsonlProvenanceLog) -> None:
    for i in range(3):
        log.append({"state": "A1", "event": "enter", "reason_code": str(i)})
    records = _records(log)
    _rewrite(log, [records[0], records[2], records[1]])
    errors = verify_provenance_chain(log.path).errors
    assert any("seq" in e for e in errors) and any("prev_hash" in e for e in errors)


def test_removed_record_detected(log: JsonlProvenanceLog) -> None:
    for _ in range(3):
        log.append({"state": "A1", "event": "enter"})
    records = _records(log)
    _rewrite(log, [records[0], records[2]])
    assert any("prev_hash" in e for e in verify_provenance_chain(log.path).errors)


def test_append_refuses_tampered_tip(log: JsonlProvenanceLog) -> None:
    log.append({"state": "A1", "event": "enter"})
    records = _records(log)
    records[-1]["state"] = "A2"
    _rewrite(log, records)
    with pytest.raises(ProvenanceError, match="hash mismatch"):
        log.append({"state": "A2", "event": "enter"})


def test_append_refuses_torn_final_record(log: JsonlProvenanceLog) -> None:
    log.append({"state": "A1", "event": "enter"})
    with log.path.open("a") as fh:
        fh.write('{"partial": ')
    with pytest.raises(ProvenanceError, match="torn"):
        log.append({"state": "A1", "event": "enter"})


def test_unresolved_call_reported(log: JsonlProvenanceLog) -> None:
    log.append(call("i1"))
    report = verify_provenance_chain(log.path)
    assert report.unresolved == ["i1"] and not report.ok


def test_exception_resolves_call(log: JsonlProvenanceLog) -> None:
    log.append(call("i1"))
    log.append({**result("i1"), "event": "exception", "reason_code": "adapter_error"})
    assert verify_provenance_chain(log.path).ok


@pytest.mark.parametrize(
    ("events", "message"),
    [
        ([result("i9")], "orphaned model_result"),
        ([call("i1"), result("i1"), result("i1")], "duplicate model_result"),
        ([call("i1"), call("i1")], "duplicate model_call"),
        ([call("i1"), result("i1", op="proposal_b")], "operation_id mismatch"),
    ],
    ids=["orphaned", "duplicate-result", "duplicate-call", "operation-mismatch"],
)
def test_correlation_errors(log: JsonlProvenanceLog, events: list[dict], message: str) -> None:
    for event in events:
        log.append(event)
    assert any(message in e for e in verify_provenance_chain(log.path).errors)


def test_concurrent_appends_form_linear_chain(tmp_path: Path) -> None:
    logs = [JsonlProvenanceLog(tmp_path, "run-1") for _ in range(4)]

    def worker(log: JsonlProvenanceLog) -> None:
        for _ in range(25):
            log.append({"state": "A1", "event": "enter"})

    threads = [threading.Thread(target=worker, args=(log,)) for log in logs]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    report = verify_provenance_chain(logs[0].path)
    assert report.ok and report.count == 100, report.errors[:3]
