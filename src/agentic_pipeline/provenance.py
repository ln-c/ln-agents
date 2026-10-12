"""L1 provenance: one append-only, SHA-256 hash-chained JSONL file per run.

Each record carries `prev_hash` (the previous record's `event_hash`, or
GENESIS_HASH for seq 0) and `event_hash` (SHA-256 of the canonical JSON of
the record without `event_hash`). Appends are serialized by a thread lock and
an exclusive `flock`, validated against `schemas/provenance_event.schema.json`,
and fsynced before returning. This makes the log tamper-evident, not
tamper-proof; immutable storage is host configuration.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from agentic_pipeline.fsutil import fsync_dir
from agentic_pipeline.schemas import ValidationError, validate

GENESIS_HASH = "sha256:" + "0" * 64
RUN_ID_RE = re.compile(r"[a-zA-Z0-9_-]{1,64}")
LOG_ASSIGNED = frozenset({"run_id", "seq", "prev_hash", "event_hash"})
CORRELATED_TERMINALS = frozenset({"model_result", "exception"})


class ProvenanceError(Exception):
    """The log could not be appended to or is corrupt; callers must fail closed."""


def canonical_hash(record: Mapping[str, Any]) -> str:
    body = {k: v for k, v in record.items() if k != "event_hash"}
    data = json.dumps(body, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return "sha256:" + hashlib.sha256(data.encode("utf-8")).hexdigest()


def log_path(directory: Path, run_id: str) -> Path:
    if not RUN_ID_RE.fullmatch(run_id):
        raise ValueError(f"invalid run_id {run_id!r}")
    return directory / f"provenance_{run_id}.jsonl"


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


class JsonlProvenanceLog:
    def __init__(self, directory: Path, run_id: str, clock: Callable[[], str] = _utc_now) -> None:
        self.path = log_path(directory, run_id)
        self.run_id = run_id
        self._clock = clock
        self._lock = threading.Lock()

    def count(self) -> int:
        """Number of records so far; the next append gets this seq."""
        try:
            return sum(1 for line in self.path.read_bytes().splitlines() if line.strip())
        except FileNotFoundError:
            return 0

    def records(self, from_seq: int = 0) -> list[dict[str, Any]]:
        """Records with seq >= from_seq (parsed, not verified; verify the chain first)."""
        try:
            lines = self.path.read_bytes().splitlines()
        except FileNotFoundError:
            return []
        return [r for r in (json.loads(x) for x in lines if x.strip()) if r["seq"] >= from_seq]

    def append(self, event: Mapping[str, Any]) -> dict[str, Any]:
        """Append one event, assigning run_id, seq, timestamp, and hashes."""
        if clash := LOG_ASSIGNED & event.keys():
            raise ValueError(f"fields assigned by the log: {sorted(clash)}")
        with self._lock:
            created = not self.path.exists()
            fd = os.open(self.path, os.O_RDWR | os.O_CREAT | os.O_APPEND, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX)
                record = self._next_record(fd, event)
                _write_all(fd, (json.dumps(record, ensure_ascii=False) + "\n").encode("utf-8"))
                os.fsync(fd)
            finally:
                os.close(fd)  # closing releases the flock
            if created:
                fsync_dir(self.path.parent)
        return record

    def _next_record(self, fd: int, event: Mapping[str, Any]) -> dict[str, Any]:
        data = os.pread(fd, os.fstat(fd).st_size, 0)
        seq, prev_hash = 0, GENESIS_HASH
        if data.strip():
            if not data.endswith(b"\n"):
                raise ProvenanceError(f"{self.path}: torn final record")
            try:
                tip = json.loads(data.rstrip(b"\n").rsplit(b"\n", 1)[-1])
            except ValueError as exc:
                raise ProvenanceError(f"{self.path}: unreadable final record") from exc
            if canonical_hash(tip) != tip.get("event_hash"):
                raise ProvenanceError(f"{self.path}: final record hash mismatch")
            seq, prev_hash = tip["seq"] + 1, tip["event_hash"]
        record = {
            "timestamp": self._clock(),
            **event,
            "run_id": self.run_id,
            "seq": seq,
            "prev_hash": prev_hash,
        }
        record["event_hash"] = canonical_hash(record)
        try:
            validate("provenance_event", record)
        except ValidationError as exc:
            raise ProvenanceError(f"invalid provenance event: {exc.message}") from exc
        return record


def _write_all(fd: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        view = view[os.write(fd, view) :]


@dataclass(slots=True)
class ProvenanceReport:
    count: int = 0
    tip: str = GENESIS_HASH
    errors: list[str] = field(default_factory=list)
    unresolved: list[str] = field(default_factory=list)
    # invocation_id -> {"state", "operation_id"} for each call with no recorded outcome
    unresolved_calls: dict[str, dict[str, str]] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.errors and not self.unresolved


def verify_provenance_chain(path: Path) -> ProvenanceReport:
    """Check hash continuity, sequence, schema, and model_call resolution."""
    report = ProvenanceReport()
    open_calls: dict[str, str] = {}  # invocation_id -> operation_id
    call_states: dict[str, str] = {}
    resolved: set[str] = set()
    run_id: str | None = None
    for line_no, line in enumerate(path.read_bytes().splitlines(), start=1):
        where = f"line {line_no}"
        try:
            record = json.loads(line)
            validate("provenance_event", record)
        except (ValueError, ValidationError) as exc:
            report.errors.append(f"{where}: invalid record: {exc}")
            break
        run_id = run_id or record["run_id"]
        if record["run_id"] != run_id:
            report.errors.append(f"{where}: run_id {record['run_id']!r} != {run_id!r}")
        if record["seq"] != report.count:
            report.errors.append(f"{where}: seq {record['seq']} != expected {report.count}")
        if record["prev_hash"] != report.tip:
            report.errors.append(f"{where}: prev_hash does not match previous event_hash")
        if canonical_hash(record) != record["event_hash"]:
            report.errors.append(f"{where}: event_hash mismatch (record modified)")
        report.count += 1
        report.tip = record["event_hash"]

        invocation = record.get("invocation_id")
        if record["event"] == "model_call":
            if invocation in open_calls or invocation in resolved:
                report.errors.append(f"{where}: duplicate model_call {invocation!r}")
            open_calls[invocation] = record["operation_id"]
            call_states[invocation] = record["state"]
        elif record["event"] in CORRELATED_TERMINALS and invocation is not None:
            if invocation not in open_calls:
                kind = "duplicate" if invocation in resolved else "orphaned"
                report.errors.append(f"{where}: {kind} {record['event']} for {invocation!r}")
            elif record.get("operation_id") != open_calls[invocation]:
                report.errors.append(f"{where}: operation_id mismatch for {invocation!r}")
            else:
                del open_calls[invocation]
                resolved.add(invocation)
    report.unresolved = sorted(open_calls)
    report.unresolved_calls = {
        i: {"state": call_states[i], "operation_id": op} for i, op in open_calls.items()
    }
    return report
