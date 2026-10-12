"""Durable run state: current graph node, status, budget ledger, and human resumes.

One `state.json` per run, written atomically (fsutil) and validated against
schemas/run_state.schema.json. A `state.lock` flock keeps a single runner per run.

Status is a run-level property, not a graph state (ADR 0003, decision 5):

    running         the runner may take steps
    paused          stopped after budget was spent or an outcome is uncertain;
                    only a recorded human resume continues it
    awaiting_human  the run is at a human state (H1, R1, R2, HR)
    finished        the run reached a terminal state

Recovery is fail-closed. A run found with a reserved budget unit or an unfinished
transition (the process died mid-dispatch) is paused, and the unit is counted as
spent, because the model call may already have happened. A provenance chain that
does not verify refuses to load at all.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from agentic_pipeline.fsutil import atomic_write_json
from agentic_pipeline.provenance import JsonlProvenanceLog, verify_provenance_chain
from agentic_pipeline.schemas import ValidationError, validate

STATE_FILE = "state.json"
LOCK_FILE = "state.lock"
PROVENANCE_DIR = "provenance"


RUN_ID_RE = re.compile(r"[a-zA-Z0-9_-]{1,64}")


def validate_run_id(run_id: str) -> str:
    """Reject ids that could name anything but a single directory; call before any I/O."""
    if not RUN_ID_RE.fullmatch(run_id):
        raise RunStateError(f"invalid run id {run_id!r} (letters, digits, _ and - only)")
    return run_id


class RunStateError(Exception):
    """The run state is missing, unreadable, invalid, or inconsistent."""


class RunLocked(RunStateError):
    pass


class PolicyDrift(RunStateError):
    """Graph or policy files changed since the run started."""


class Status(StrEnum):
    RUNNING = "running"
    PAUSED = "paused"
    AWAITING_HUMAN = "awaiting_human"
    FINISHED = "finished"


def _now() -> str:
    return datetime.now(UTC).isoformat()


def policy_digests(root: Path) -> dict[str, str]:
    """SHA-256 of the graph and every policy file, frozen into the run at start."""
    files = [root / "pipeline" / "graph.yaml", *sorted((root / "policy").glob("*.yaml"))]
    return {
        p.relative_to(root).as_posix(): "sha256:" + hashlib.sha256(p.read_bytes()).hexdigest()
        for p in files
    }


@dataclass(slots=True)
class RunState:
    run_id: str
    current: str
    status: Status = Status.RUNNING
    entering: dict[str, str] | None = None
    paused_reason: str | None = None
    ledger: dict[str, dict[str, int]] = field(
        default_factory=lambda: {"consumed": {}, "reserved": {}}
    )
    policy: dict[str, str] = field(default_factory=dict)
    resumes: list[dict[str, Any]] = field(default_factory=list)
    steps: int = 0
    updated_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "version": 1,
            "run_id": self.run_id,
            "status": self.status.value,
            "current": self.current,
            "entering": self.entering,
            "paused_reason": self.paused_reason,
            "ledger": self.ledger,
            "policy": self.policy,
            "resumes": self.resumes,
            "steps": self.steps,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, doc: Mapping[str, Any]) -> RunState:
        return cls(
            run_id=doc["run_id"],
            current=doc["current"],
            status=Status(doc["status"]),
            entering=dict(doc["entering"]) if doc["entering"] else None,
            paused_reason=doc["paused_reason"],
            ledger={k: dict(v) for k, v in doc["ledger"].items()},
            policy=dict(doc["policy"]),
            resumes=list(doc["resumes"]),
            steps=doc["steps"],
            updated_at=doc["updated_at"],
        )

    def pause(self, reason: str) -> None:
        self.status = Status.PAUSED
        self.paused_reason = reason


class RunStore:
    """Persistence for one run directory."""

    def __init__(self, run_root: Path) -> None:
        self.run_root = run_root
        self.path = run_root / STATE_FILE
        self._lock_fd: int | None = None

    @property
    def provenance_dir(self) -> Path:
        return self.run_root / PROVENANCE_DIR

    def exists(self) -> bool:
        return self.path.is_file()

    def acquire(self) -> None:
        """Take the single-runner lock; raises RunLocked if another process holds it."""
        self.run_root.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.run_root / LOCK_FILE, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            os.close(fd)
            raise RunLocked(f"{self.run_root} is being run by another process") from exc
        self._lock_fd = fd

    def release(self) -> None:
        if self._lock_fd is not None:
            os.close(self._lock_fd)  # closing releases the flock
            self._lock_fd = None

    def load(self) -> RunState:
        try:
            doc = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise RunStateError(f"no run state at {self.path}") from exc
        except (OSError, ValueError) as exc:
            raise RunStateError(f"unreadable run state: {exc}") from exc
        try:
            validate("run_state", doc)
        except ValidationError as exc:
            raise RunStateError(f"invalid run state: {exc.message}") from exc
        return RunState.from_dict(doc)

    def save(self, state: RunState) -> None:
        state.updated_at = _now()
        doc = state.to_dict()
        try:
            validate("run_state", doc)
        except ValidationError as exc:
            raise RunStateError(f"refusing to write invalid run state: {exc.message}") from exc
        atomic_write_json(self.path, doc)

    def create(self, state: RunState) -> None:
        if self.exists():
            raise RunStateError(f"run already exists: {self.path}")
        self.save(state)


def check_policy(state: RunState, root: Path) -> None:
    now = policy_digests(root)
    if now != state.policy:
        changed = sorted(
            k for k in now.keys() | state.policy.keys() if now.get(k) != state.policy.get(k)
        )
        raise PolicyDrift(f"policy changed since the run started: {changed}")


def recover(state: RunState, log: JsonlProvenanceLog) -> list[str]:
    """Apply fail-closed recovery in place; return notes describing what was changed."""
    notes: list[str] = []
    if state.status is Status.FINISHED:
        return notes

    reserved = {k: v for k, v in state.ledger["reserved"].items() if v}
    if reserved or state.entering:
        for budget, count in reserved.items():
            state.ledger["consumed"][budget] = state.ledger["consumed"].get(budget, 0) + count
        state.ledger["reserved"] = {}
        notes.append(
            f"unfinished dispatch (reserved={reserved}, entering={state.entering}): "
            "reserved units counted as spent"
        )
        state.entering = None
        if state.status is Status.RUNNING:
            state.pause("crash_during_dispatch")

    if log.path.exists():
        report = verify_provenance_chain(log.path)
        if report.errors:
            raise RunStateError(f"provenance chain does not verify: {report.errors[:3]}")
        if report.unresolved and state.status is Status.RUNNING:
            state.pause("unresolved_invocation")
            notes.append(f"model calls with no recorded outcome: {report.unresolved}")
    return notes


def resume(state: RunState, log: JsonlProvenanceLog, by: str, reason: str) -> None:
    """Record a human decision to continue a paused run.

    Identity is self-asserted: the CLI cannot authenticate `by`. Any model call that
    never recorded an outcome is closed with a `human_reconciled` exception event so
    the provenance log verifies again, then a `decision` event records the resume.
    """
    if state.status is not Status.PAUSED:
        raise RunStateError(f"run is {state.status.value}, not paused")
    if not by.strip() or not reason.strip():
        raise RunStateError("a resume needs --by and --reason")
    if log.path.exists():
        for invocation_id, call in sorted(
            verify_provenance_chain(log.path).unresolved_calls.items()
        ):
            log.append(
                {
                    "state": call["state"],
                    "event": "exception",
                    "operation_id": call["operation_id"],
                    "invocation_id": invocation_id,
                    "reason_code": "human_reconciled",
                    "approver_id": by,
                }
            )
    event = log.append(
        {
            "state": state.current,
            "event": "decision",
            "reason_code": "human_resume",
            "approver_id": by,
        }
    )
    state.resumes.append(
        {
            "by": by,
            "reason": reason,
            "at": _now(),
            "paused_reason": state.paused_reason,
            "provenance_tip": event["event_hash"],
        }
    )
    state.status = Status.RUNNING
    state.paused_reason = None
    state.entering = None
