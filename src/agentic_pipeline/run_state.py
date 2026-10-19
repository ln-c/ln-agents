"""Durable run state: current graph node, status, budget ledger, and human resumes.

One `state.json` per run, written atomically (fsutil) and validated against
schemas/run_state.schema.json. A `state.lock` flock keeps a single runner per run.

Status is a run-level property, not a graph state (a dispatch failure with no graph edge
pauses the run; it never routes anywhere without a human):

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

import hashlib
import json
import os
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

import yaml

from agentic_pipeline.fsutil import O_BINARY, atomic_write_json, lock, retry_sharing, unlock
from agentic_pipeline.graph import Graph, loads_yaml
from agentic_pipeline.provenance import RUN_ID_RE, JsonlProvenanceLog, verify_provenance_chain
from agentic_pipeline.schemas import ValidationError, validate

STATE_FILE = "state.json"
LOCK_FILE = "state.lock"
PROVENANCE_DIR = "provenance"
FROZEN_DIR = "frozen"
LOCK_WAIT_SECONDS = 1.0


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
    """The graph or policy a run reads no longer matches the digests it recorded at start."""


class PauseCause(StrEnum):
    """Why a run paused; resume decisions use this, never the free-text reason."""

    CRASH_DURING_DISPATCH = "crash_during_dispatch"
    UNRESOLVED_INVOCATION = "unresolved_invocation"
    BUDGET_EXHAUSTED = "budget_exhausted"
    DISPATCH_PAUSED = "dispatch_paused"  # quota, auth, or adapter failure after spending
    PROVENANCE_UNAVAILABLE = "provenance_unavailable"
    STATE_SAVE_FAILED = "state_save_failed"
    UNEXPECTED_ERROR = "unexpected_error"
    OTHER = "other"


class Status(StrEnum):
    RUNNING = "running"
    PAUSED = "paused"
    AWAITING_HUMAN = "awaiting_human"
    FINISHED = "finished"


def _now() -> str:
    return datetime.now(UTC).isoformat()


POLICY_PARSED = (
    "policy/model_registry.yaml",
    "policy/budgets.yaml",
    "policy/data_classes.yaml",
    "policy/task_classes.yaml",
    "policy/weights.yaml",
    "policy/vetoes.yaml",
    "policy/fast_path.yaml",
    "policy/escalation.yaml",
    "policy/routing.yaml",
    "policy/advisories.yaml",
    "policy/approvers.yaml",
    "policy/ci.yaml",
    "policy/release.yaml",
    "policy/triage.yaml",
)


@dataclass(frozen=True, slots=True)
class PolicySnapshot:
    """Graph and policy as checked: digests and parsed documents come from the same bytes."""

    digests: dict[str, str]
    graph: Graph
    registry: Mapping[str, Any]
    budgets: Mapping[str, Any]
    data_classes: Mapping[str, Any]
    task_classes: Mapping[str, Any]
    weights: Mapping[str, Any]
    vetoes: Mapping[str, Any]
    fast_path: Mapping[str, Any]
    escalation: Mapping[str, Any]
    routing: Mapping[str, Any]
    advisories: Mapping[str, Any]
    approvers: Mapping[str, Any]
    ci: Mapping[str, Any]
    release: Mapping[str, Any]
    triage: Mapping[str, Any]


def read_policy(root: Path) -> dict[str, bytes]:
    """The graph and every policy file under `root`, as bytes, keyed by their relative path."""
    files = [root / "pipeline" / "graph.yaml", *sorted((root / "policy").glob("*.yaml"))]
    try:
        return {p.relative_to(root).as_posix(): p.read_bytes() for p in files}
    except OSError as exc:
        raise RunStateError(f"cannot read graph or policy: {type(exc).__name__}: {exc}") from exc


def parse_policy(raw: Mapping[str, bytes]) -> PolicySnapshot:
    """Hash and parse the bytes `read_policy` returned (the digests and documents agree)."""
    try:
        parsed = {rel: loads_yaml(raw[rel]) for rel in ("pipeline/graph.yaml", *POLICY_PARSED)}
        return PolicySnapshot(
            digests={rel: "sha256:" + hashlib.sha256(d).hexdigest() for rel, d in raw.items()},
            graph=Graph.from_mapping(parsed["pipeline/graph.yaml"]),
            registry=parsed["policy/model_registry.yaml"],
            budgets=parsed["policy/budgets.yaml"],
            data_classes=parsed["policy/data_classes.yaml"],
            task_classes=parsed["policy/task_classes.yaml"],
            weights=parsed["policy/weights.yaml"],
            vetoes=parsed["policy/vetoes.yaml"],
            fast_path=parsed["policy/fast_path.yaml"],
            escalation=parsed["policy/escalation.yaml"],
            routing=parsed["policy/routing.yaml"],
            advisories=parsed["policy/advisories.yaml"],
            approvers=parsed["policy/approvers.yaml"],
            ci=parsed["policy/ci.yaml"],
            release=parsed["policy/release.yaml"],
            triage=parsed["policy/triage.yaml"],
        )
    except (KeyError, TypeError, AttributeError, ValueError, yaml.YAMLError) as exc:
        raise RunStateError(f"cannot read graph or policy: {type(exc).__name__}: {exc}") from exc


def load_policy(root: Path) -> PolicySnapshot:
    """Read the graph and every policy file once; hash and parse those exact bytes."""
    return parse_policy(read_policy(root))


def write_frozen_policy(raw: Mapping[str, bytes], run_root: Path) -> None:
    """Put the policy a run starts under into `<run>/frozen`, byte for byte.

    `raw` is what `parse_policy` hashed, so the run's recorded digests describe exactly these
    files. Later edits to the repository's policy do not reach the run.
    """
    for rel, data in raw.items():
        target = run_root / FROZEN_DIR / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)


def run_policy_root(root: Path, run_root: Path) -> Path:
    """Where a run reads its graph and policy: its own frozen copy, or `root` for a run that
    began before runs froze a copy (it is then drift-checked against the repository)."""
    frozen = run_root / FROZEN_DIR
    return frozen if frozen.is_dir() else root


def load_run_policy(root: Path, run_root: Path) -> PolicySnapshot:
    return load_policy(run_policy_root(root, run_root))


@dataclass(slots=True)
class RunState:
    run_id: str
    current: str
    status: Status = Status.RUNNING
    # crash marker: from, guard, to, after_seq, consumed_before
    entering: dict[str, Any] | None = None
    paused_reason: str | None = None
    pause_cause: PauseCause | None = None
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
            "pause_cause": self.pause_cause.value if self.pause_cause else None,
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
            pause_cause=PauseCause(doc["pause_cause"]) if doc["pause_cause"] else None,
            ledger={k: dict(v) for k, v in doc["ledger"].items()},
            policy=dict(doc["policy"]),
            resumes=list(doc["resumes"]),
            steps=doc["steps"],
            updated_at=doc["updated_at"],
        )

    def pause(self, reason: str, cause: PauseCause = PauseCause.OTHER) -> None:
        self.status = Status.PAUSED
        self.paused_reason = reason
        self.pause_cause = cause


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

    def acquire(self, wait: float = LOCK_WAIT_SECONDS) -> None:
        """Take the single-runner lock; raises RunLocked if it is still held after `wait`.

        The short wait rides out momentary holders such as `run status` probing it.
        """
        self.run_root.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.run_root / LOCK_FILE, os.O_RDWR | os.O_CREAT | O_BINARY, 0o600)
        deadline = time.monotonic() + wait
        while True:
            try:
                lock(fd, blocking=False)
                break
            except BlockingIOError as exc:
                if time.monotonic() >= deadline:
                    os.close(fd)
                    raise RunLocked(f"{self.run_root} is being run by another process") from exc
                time.sleep(0.05)
        self._lock_fd = fd

    def is_locked(self) -> bool:
        """True if another process holds the runner lock.

        Probing takes the lock for an instant; acquire() waits briefly, so a runner
        starting at the same moment is not refused.
        """
        path = self.run_root / LOCK_FILE
        if not path.exists():
            return False
        fd = os.open(path, os.O_RDWR | O_BINARY)
        try:
            lock(fd, blocking=False)
        except BlockingIOError:
            return True
        else:
            unlock(fd)
        finally:
            os.close(fd)
        return False

    def release(self) -> None:
        if self._lock_fd is not None:
            unlock(self._lock_fd)
            os.close(self._lock_fd)
            self._lock_fd = None

    def load(self) -> RunState:
        try:
            # Read while a runner may be replacing it (the A2A server polls during `advance`).
            doc = json.loads(retry_sharing(lambda: self.path.read_text(encoding="utf-8")))
        except FileNotFoundError as exc:
            raise RunStateError(f"no run state at {self.path}") from exc
        except (OSError, ValueError) as exc:
            raise RunStateError(f"unreadable run state: {exc}") from exc
        try:
            validate("run_state", doc)
        except ValidationError as exc:
            raise RunStateError(f"invalid run state: {exc.message}") from exc
        if doc["run_id"] != self.run_root.name:
            raise RunStateError(
                f"{self.path} belongs to run {doc['run_id']!r}, not {self.run_root.name!r}; "
                "copied or renamed run directories are refused"
            )
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


def check_policy(state: RunState, policy: PolicySnapshot) -> None:
    now = policy.digests
    if now != state.policy:
        changed = sorted(
            k for k in now.keys() | state.policy.keys() if now.get(k) != state.policy.get(k)
        )
        raise PolicyDrift(f"policy changed since the run started: {changed}")


def recover(state: RunState, log: JsonlProvenanceLog) -> list[str]:
    """Apply fail-closed recovery in place; return notes describing what was changed.

    A transition is committed by its `enter` provenance event. If the crash marker
    (`entering`) is still set but an `enter` for its target was logged after the
    marker was written, the stage finished and the run advances; nothing is re-run.
    Otherwise the run pauses, because model calls may already have happened.
    """
    notes: list[str] = []
    if state.status is Status.FINISHED:
        return notes
    report = verify_provenance_chain(log.path) if log.path.exists() else None
    if report is not None and report.errors:
        raise RunStateError(f"provenance chain does not verify: {report.errors[:3]}")

    reserved = {k: v for k, v in state.ledger["reserved"].items() if v}
    if reserved:
        for budget, count in reserved.items():
            state.ledger["consumed"][budget] = state.ledger["consumed"].get(budget, 0) + count
        state.ledger["reserved"] = {}
        notes.append(f"reserved units {reserved} counted as spent")

    marker = state.entering
    if marker is not None:
        state.entering = None
        committed = any(
            r["event"] == "enter" and r["state"] == marker["to"]
            for r in log.records(marker["after_seq"])
        )
        untouched = (
            not reserved
            and not log.records(marker["after_seq"])
            and state.ledger["consumed"] == marker["consumed_before"]
        )
        if committed:
            state.current = marker["to"]
            state.steps += 1
            notes.append(f"transition {marker['from']} -> {marker['to']} had completed; advanced")
        elif untouched:
            # Nothing was reserved, spent, or logged after the marker: a stale marker
            # (e.g. a blocked step whose cleanup save failed), not a crash.
            notes.append(f"stale marker {marker['from']} -> {marker['to']} cleared; nothing ran")
        else:
            notes.append(f"transition {marker['from']} -> {marker['to']} did not complete")
            if state.status is Status.RUNNING:
                state.pause("crash_during_dispatch", PauseCause.CRASH_DURING_DISPATCH)
    elif reserved and state.status is Status.RUNNING:
        state.pause("crash_during_dispatch", PauseCause.CRASH_DURING_DISPATCH)

    if report is not None and report.unresolved and state.status is Status.RUNNING:
        state.pause("unresolved_invocation", PauseCause.UNRESOLVED_INVOCATION)
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
                    "approver_id": f"unverified:{by}",
                }
            )
    event = log.append(
        {
            "state": state.current,
            "event": "decision",
            "reason_code": "human_resume",
            "approver_id": f"unverified:{by}",
        }
    )
    state.resumes.append(
        {
            "by": by,
            "identity": "self-asserted",
            "reason": reason,
            "at": _now(),
            "paused_reason": state.paused_reason,
            "provenance_tip": event["event_hash"],
        }
    )
    state.status = Status.RUNNING
    state.paused_reason = None
    state.pause_cause = None
    state.entering = None
