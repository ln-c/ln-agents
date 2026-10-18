"""The orchestrator as an A2A agent (`ln-pipeline`): runs driven over A2A JSON-RPC and SSE.

`python -m agentic_pipeline serve a2a` serves, on a loopback address only:

* `/.well-known/agent-card.json`, public: the agent card;
* `/a2a/jsonrpc`, behind a bearer token: SendMessage, SendStreamingMessage, GetTask,
  ListTasks, CancelTask (a2a-sdk's JSON-RPC binding, protocol 1.0).

A request is one A2A message whose single data part names a skill: `start_run`, `advance`,
`status`, `evidence` or `approval_document`. Every skill calls the same code as the CLI. None
of them can sign an approval, take a human edge, resume a paused run or change policy: a human
signs with ssh-keygen and runs `run decide` (or `run resume`). An A2A message never approves.

A request is checked, and `start_run` creates its run, before any task is touched, so a refused
request is a JSON-RPC error and leaves every task as it was (the SDK fails a task terminally
when its executor raises). `status`, `evidence` and `approval_document` are queries: they are
answered with a message, never through a task, even when the message names one. A message that
names a task without a context gets the task's context.

A run is advanced under one A2A task. While it advances, every provenance event is streamed as
a WORKING status update; when it stops, the task's state says why:

* awaiting a human (H1, R1, R2) -> INPUT_REQUIRED, with the evidence as an artifact;
* paused on `auth_failed` or `quota_exhausted` -> AUTH_REQUIRED;
* paused otherwise, blocked, or out of steps -> INPUT_REQUIRED (the run is resumable; FAILED
  would be terminal in A2A);
* finished -> COMPLETED (CANCELED at T-CANCELED).

INPUT_REQUIRED covers several stops, so the final status message also carries a data part
`{"run_status": ..., "node": ...}`: `awaiting_human`, `paused`, `blocked` (the step spent
nothing; advance retries it, and it blocks again until the cause is fixed),
`out_of_steps`, `stopped` (known only from the run, after a restart) or `finished`.

Tasks are stored next to the runs (`<runs>/.a2a/tasks`). A stored task that claims to be
running when no request is driving it (the server restarted mid-advance) is answered from the
run's own state, so it never reads as WORKING forever. The run lock is held only while a request
is actually stepping the run, so `run decide` and `run status` keep working alongside.
Cancelling stops the run after its current step; a server killed mid-step leaves the run's crash
marker, and the next open pauses it for a human, as for any interrupted step.
"""

from __future__ import annotations

import asyncio
import hmac
import ipaddress
import json
import logging
import os
import secrets
import socket
import tempfile
import threading
from collections.abc import AsyncGenerator, Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from importlib import metadata
from pathlib import Path
from typing import Any

import uvicorn
from a2a.helpers.proto_helpers import (
    get_data_parts,
    new_data_message,
    new_data_part,
    new_message,
    new_task_from_user_message,
    new_text_part,
)
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.context import ServerCallContext
from a2a.server.events import EventQueue
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import create_agent_card_routes, create_jsonrpc_routes
from a2a.server.tasks import InMemoryTaskStore, TaskStore, TaskUpdater
from a2a.types.a2a_pb2 import (
    AgentCapabilities,
    AgentCard,
    AgentInterface,
    AgentSkill,
    HTTPAuthSecurityScheme,
    ListTasksRequest,
    ListTasksResponse,
    Message,
    SecurityRequirement,
    SecurityScheme,
    SendMessageRequest,
    StringList,
    Task,
    TaskState,
)
from a2a.utils.constants import AGENT_CARD_WELL_KNOWN_PATH
from a2a.utils.errors import InvalidParamsError, TaskNotFoundError
from google.protobuf.json_format import MessageToDict, ParseDict
from starlette.applications import Starlette
from starlette.responses import JSONResponse

from agentic_pipeline.coordinator import Coordinator, StopInfo
from agentic_pipeline.coordinator import for_run as coordinator_for_run
from agentic_pipeline.fsutil import WINDOWS, atomic_write_json
from agentic_pipeline.provenance import JsonlProvenanceLog
from agentic_pipeline.run_cli import (
    _dispatcher_factory,
    save_evidence_report,
    write_approval_document,
)
from agentic_pipeline.run_state import RunLocked, RunStateError, RunStore, Status, validate_run_id
from agentic_pipeline.runner import (
    MakeDispatcher,
    Outcome,
    StateHandler,
    StepResult,
    init_run,
    open_run,
)

AGENT_NAME = "ln-pipeline"
RPC_PATH = "/a2a/jsonrpc"
DEFAULT_PORT = 8420
TASKS_DIR = ".a2a"  # under the runs directory; not a valid run id, so never listed as a run
POLL_SECONDS = 1.0
DEFAULT_MAX_STEPS = 100
CREDENTIAL_REASONS = frozenset({"auth_failed", "quota_exhausted"})
RUN_ID_KEY = "run_id"  # the task metadata field that names the run it advances

# skill -> (required arguments, optional arguments); anything else is refused.
SKILLS: Mapping[str, tuple[frozenset[str], frozenset[str]]] = {
    "start_run": (frozenset({"run_id", "entry_spec"}), frozenset({"entry", "max_steps"})),
    "advance": (frozenset(), frozenset({"run_id", "max_steps"})),
    "status": (frozenset(), frozenset({"run_id"})),
    "evidence": (frozenset(), frozenset({"run_id"})),
    "ask": (frozenset({"question"}), frozenset({"run_id"})),
    "approval_document": (
        frozenset({"decision", "approver", "reason"}),
        frozenset({"run_id", "candidate", "remediation_cause"}),
    ),
}
SKILL_TEXT = {
    "start_run": "Create a run from an entry spec at START, then advance it.",
    "advance": "Advance a run until it needs a human, pauses, finishes or runs out of steps.",
    "status": "The run's state: where it is, its status, budget and pause reason.",
    "evidence": (
        "What a human decides on at H1, R1 or R2: the evidence they sign, the choices, who may "
        "sign, and a readable Markdown report (written to the run's approvals/ folder)."
    ),
    "ask": (
        "Ask the coordinator model a question about a run. It answers in text from the run's "
        "Markdown views and changes nothing."
    ),
    "approval_document": (
        "Write the unsigned approval document and return the ssh-keygen and `run decide` "
        "commands. The human signs and decides; nothing here approves."
    ),
}


class ServeError(Exception):
    pass


def _event_text(event: Mapping[str, Any]) -> str:
    """One line for a provenance event, as an editor would show it."""
    state, kind = event.get("state", "?"), event.get("event", "?")
    op = f".{event['operation_id']}" if event.get("operation_id") else ""
    if kind == "enter":
        return f"entered {state}"
    if kind == "model_call":
        return f"{state}{op}: calling {event.get('model', '?')}"
    if kind == "model_result":
        return f"{state}{op}: {event.get('reason_code', '?')}"
    return f"{state}{op}: {kind} {event.get('reason_code', '')}".rstrip()


# ── The pipeline, as plain functions over the runs directory ────────────────


@dataclass(slots=True)
class PipelineService:
    """What the skills do, with no A2A types: the same functions the CLI uses."""

    root: Path
    runs_dir: Path
    make_dispatcher: Callable[[Path, Path], MakeDispatcher] = _dispatcher_factory
    handlers: Mapping[str, StateHandler] | None = None
    # (root, run directory) -> the run's coordinator: the model that writes the Markdown views
    coordinator_for: Callable[[Path, Path], Coordinator] = coordinator_for_run
    _converting: set[str] = field(default_factory=set)
    _converting_lock: threading.Lock = field(default_factory=threading.Lock)

    def run_dir(self, run_id: str) -> Path:
        validate_run_id(run_id)
        return self.runs_dir / run_id

    def start(self, run_id: str, entry: str, entry_spec: Mapping[str, Any]) -> None:
        with tempfile.TemporaryDirectory(prefix="ln-pipeline-spec-") as tmp:
            spec = Path(tmp) / "entry_spec.json"
            spec.write_text(json.dumps(entry_spec), encoding="utf-8")
            init_run(self.root, self.run_dir(run_id), run_id, entry, spec)

    def advance(self, run_id: str, max_steps: int, stop: threading.Event) -> StepResult:
        """Step the run until it stops advancing, `max_steps`, or `stop`; the lock is released
        before this returns, so a human can decide while the task waits."""
        run_dir = self.run_dir(run_id)
        try:
            runner = open_run(
                self.root, run_dir, self.make_dispatcher(self.root, run_dir), self.handlers
            )
        except RunLocked as exc:
            return StepResult(Outcome.BLOCKED, RunStore(run_dir).load().current, str(exc))
        try:
            result = StepResult(Outcome.BLOCKED, runner.state.current, "no steps taken")
            for _ in range(max_steps):
                if stop.is_set():
                    return StepResult(Outcome.BLOCKED, runner.state.current, "cancelled")
                result = runner.step()
                if result.outcome is not Outcome.ADVANCED:
                    break
            return result
        finally:
            runner.close()

    def events(self, run_id: str, from_seq: int) -> list[dict[str, Any]]:
        state = RunStore(self.run_dir(run_id)).load()
        log = JsonlProvenanceLog(RunStore(self.run_dir(run_id)).provenance_dir, state.run_id)
        return log.records(from_seq)

    def status(self, run_id: str) -> dict[str, Any]:
        store = RunStore(self.run_dir(run_id))
        return {**store.load().to_dict(), "runner_active": store.is_locked()}

    def coordinator(self, run_id: str) -> Coordinator:
        return self.coordinator_for(self.root, self.run_dir(run_id))

    def ask(self, run_id: str, question: str) -> dict[str, Any]:
        """The coordinator's answer to a question about a run. Text only: it changes nothing."""
        answer, sources = self.coordinator(run_id).ask(question, self.status(run_id))
        return {"answer": answer, "sources": sources}

    def stop_view(
        self, run_id: str, result: StepResult, report: Mapping[str, Any] | None
    ) -> dict[str, Any]:
        """Write the summary of the stop `result` ended in; `report` is the evidence view."""
        _, why, stop = self.task_state(run_id, result)
        info = StopInfo(stop["run_status"], stop["node"], why)
        path = self.coordinator(run_id).stop_summary(
            info,
            self.status(run_id),
            report["markdown"] if report else None,
            report["path"] if report else None,
        )
        return {"kind": info.kind, "path": str(path.resolve()), "markdown": path.read_text("utf-8")}

    def convert_in_background(self, run_id: str) -> threading.Thread | None:
        """Write a Markdown view of each new sealed document on a thread, so a stop is not
        held up by one model call per document. One conversion runs per run at a time."""
        with self._converting_lock:
            if run_id in self._converting:
                return None
            self._converting.add(run_id)

        def convert() -> None:
            store = RunStore(self.run_dir(run_id))
            try:
                # Stop as soon as a runner is stepping the run: its own model calls come first,
                # and two subscription sandboxes should not share one login at once.
                self.coordinator(run_id).convert_all(cancelled=store.is_locked)
            except Exception as exc:  # advisory: the next stop tries again
                logging.getLogger(__name__).warning("views of %s not written: %s", run_id, exc)
            finally:
                with self._converting_lock:
                    self._converting.discard(run_id)

        thread = threading.Thread(target=convert, name=f"views-{run_id}", daemon=True)
        thread.start()
        return thread

    def evidence(self, run_id: str) -> dict[str, Any]:
        """The approval view (evidence, choices, approvers, Markdown) and the report's path."""
        run_dir = self.run_dir(run_id)
        runner = open_run(self.root, run_dir, self.make_dispatcher(self.root, run_dir))
        try:
            view = runner.approval_view()
        finally:
            runner.close()
        return {**view, "path": str(save_evidence_report(run_dir, view).resolve())}

    def approval_document(self, run_id: str, **fields: Any) -> dict[str, Any]:
        from agentic_pipeline.approvals import signing_command

        draft = write_approval_document(self.root, self.run_dir(run_id), **fields)
        return {
            "node": draft.node,
            "decision": fields["decision"],
            "document": str(draft.path),
            "sign": signing_command(draft.path, draft.namespace),
            "then": f"python -m agentic_pipeline run decide {run_id} --approval {draft.path}",
        }

    def task_state(
        self, run_id: str, result: StepResult | None = None
    ) -> tuple[int, str, dict[str, str]]:
        """(A2A task state, why, {run_status, node}) for the run as it is on disk now;
        `result` is how the last advance ended, if one just did."""
        state = RunStore(self.run_dir(run_id)).load()
        node = state.current
        stop = lambda kind: {"run_status": kind, "node": node}  # noqa: E731
        if state.status is Status.FINISHED:
            done = TaskState.TASK_STATE_CANCELED if node == "T-CANCELED" else None
            return done or TaskState.TASK_STATE_COMPLETED, f"finished at {node}", stop("finished")
        if state.status is Status.AWAITING_HUMAN:
            return (
                TaskState.TASK_STATE_INPUT_REQUIRED,
                f"waiting for a signed approval at {node}: write it (approval_document skill or "
                "`run approve`), sign it with ssh-keygen, then `run decide`. An A2A message "
                "cannot approve.",
                stop("awaiting_human"),
            )
        if state.status is Status.PAUSED:
            results = [e for e in self.events(run_id, 0) if e.get("event") == "model_result"]
            reason = results[-1].get("reason_code") if results else None
            why = state.paused_reason or ""
            if reason in CREDENTIAL_REASONS:
                return (
                    TaskState.TASK_STATE_AUTH_REQUIRED,
                    f"paused at {node}: {reason}. Sign in or wait for the quota, then a human "
                    f"runs `run resume`. ({why})",
                    stop("paused"),
                )
            return (
                TaskState.TASK_STATE_INPUT_REQUIRED,
                f"paused at {node}: {why}. A human resumes it with `run resume`.",
                stop("paused"),
            )
        if result is not None and result.outcome is Outcome.BLOCKED:
            return (
                TaskState.TASK_STATE_INPUT_REQUIRED,
                f"blocked at {node}: {result.detail}. The step spent nothing; advance retries "
                "it, and it blocks again until the cause is fixed.",
                stop("blocked"),
            )
        if result is not None and result.outcome is Outcome.ADVANCED:
            return (
                TaskState.TASK_STATE_INPUT_REQUIRED,
                f"stopped at {node} after the step limit. Send advance to continue.",
                stop("out_of_steps"),
            )
        return (
            TaskState.TASK_STATE_INPUT_REQUIRED,
            f"stopped at {node}. Send advance to continue.",
            stop("stopped"),
        )


# ── Task storage next to the runs ───────────────────────────────────────────


def _single_owner(context: ServerCallContext) -> str:
    return AGENT_NAME  # one token, one user: every task belongs to the token holder


@dataclass(slots=True)
class RunTaskStore(TaskStore):
    """Tasks as JSON files under `<runs>/.a2a/tasks`, kept in memory for listing.

    A task stored as SUBMITTED or WORKING that no request is driving is answered from its run's
    state on disk, never from the stale stored status.
    """

    directory: Path
    service: PipelineService
    live: Callable[[str], bool]
    _memory: InMemoryTaskStore = field(
        default_factory=lambda: InMemoryTaskStore(owner_resolver=_single_owner)
    )
    _loaded: bool = False

    async def _load(self, context: ServerCallContext) -> None:
        if self._loaded:
            return
        self._loaded = True
        for path in sorted(self.directory.glob("*.json")):
            task = ParseDict(json.loads(path.read_text("utf-8")), Task())
            await self._memory.save(task, context)

    def _reconciled(self, task: Task) -> Task:
        running = (TaskState.TASK_STATE_SUBMITTED, TaskState.TASK_STATE_WORKING)
        run_id = task.metadata[RUN_ID_KEY] if RUN_ID_KEY in task.metadata else None
        if task.status.state not in running or self.live(task.id) or not run_id:
            return task
        fixed = Task()
        fixed.CopyFrom(task)
        state, why, stop = self.service.task_state(str(run_id))
        fixed.status.state = state
        fixed.status.message.CopyFrom(
            new_message([new_text_part(why), new_data_part(stop)], task.context_id, task.id)
        )
        return fixed

    async def save(self, task: Task, context: ServerCallContext) -> None:
        await self._load(context)
        await self._memory.save(task, context)
        atomic_write_json(self.directory / f"{task.id}.json", MessageToDict(task))

    async def get(self, task_id: str, context: ServerCallContext) -> Task | None:
        await self._load(context)
        task = await self._memory.get(task_id, context)
        return None if task is None else self._reconciled(task)

    async def list(self, params: ListTasksRequest, context: ServerCallContext) -> ListTasksResponse:
        await self._load(context)
        found = await self._memory.list(params, context)
        reconciled = [self._reconciled(t) for t in found.tasks]
        del found.tasks[:]
        found.tasks.extend(reconciled)
        return found

    async def delete(self, task_id: str, context: ServerCallContext) -> None:
        await self._load(context)
        await self._memory.delete(task_id, context)
        (self.directory / f"{task_id}.json").unlink(missing_ok=True)


# ── The executor: A2A messages to skills ────────────────────────────────────


QUERIES = frozenset({"status", "evidence", "approval_document", "ask"})


def _request(message: Message | None) -> tuple[str, dict[str, Any]]:
    """(skill, arguments) from the single data part; anything else is refused."""
    data = get_data_parts(message.parts) if message is not None else []
    if len(data) != 1 or len(message.parts) != 1 or not isinstance(data[0], dict):
        raise InvalidParamsError(message="send exactly one data part: {'skill': ..., ...}")
    args = dict(data[0])
    skill = args.pop("skill", None)
    if skill not in SKILLS:
        raise InvalidParamsError(message=f"unknown skill {skill!r}; skills: {sorted(SKILLS)}")
    required, optional = SKILLS[skill]
    if missing := sorted(required - args.keys()):
        raise InvalidParamsError(message=f"{skill} needs {missing}")
    if extra := sorted(args.keys() - required - optional):
        raise InvalidParamsError(message=f"{skill} does not take {extra}")
    return skill, args


def _max_steps(args: Mapping[str, Any]) -> int:
    value = args.get("max_steps", DEFAULT_MAX_STEPS)
    if isinstance(value, float) and value.is_integer():
        value = int(value)  # JSON numbers arrive as floats through protobuf Struct
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise InvalidParamsError(message="max_steps must be a positive integer")
    return value


def _run_of(args: Mapping[str, Any], task: Task | None) -> str | None:
    if args.get("run_id"):
        return str(args["run_id"])
    if task is not None and RUN_ID_KEY in task.metadata:
        return str(task.metadata[RUN_ID_KEY])
    return None


@dataclass(slots=True)
class PipelineExecutor(AgentExecutor):
    service: PipelineService
    poll_seconds: float = POLL_SECONDS
    _stops: dict[str, threading.Event] = field(default_factory=dict)

    def is_live(self, task_id: str) -> bool:
        return task_id in self._stops

    async def prepare(self, message: Message, task: Task | None) -> Message | None:
        """Check a request before any task is touched; answer a query outright.

        Raises InvalidParamsError for anything refused. For `start_run` the run is created
        here. Returns the reply for a query, or None when the request advances a run (then the
        SDK runs `execute` on a task).
        """
        skill, args = _request(message)
        if skill == "start_run":
            if task is not None:
                raise InvalidParamsError(message="start_run begins a new task; send it without one")
            if not isinstance(args["entry_spec"], dict):
                raise InvalidParamsError(message="entry_spec must be a JSON object")
            _max_steps(args)
            await self._call(
                self.service.start, args["run_id"], args.get("entry", "START"), args["entry_spec"]
            )
        run_id = _run_of(args, task)
        if run_id is None:
            raise InvalidParamsError(message=f"{skill} needs a run_id (or a task that has one)")
        self._check_run(run_id)
        if skill not in QUERIES:
            _max_steps(args)
            return None
        fields = {k: v for k, v in args.items() if k != "run_id"}
        result = await self._call(getattr(self.service, skill), run_id, **fields)
        return new_data_message(
            {"skill": skill, **_json(result)},
            context_id=message.context_id or None,
            task_id=message.task_id or None,
        )

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        """Advance the run (`start_run` or `advance`, already checked by `prepare`)."""
        skill, args = _request(context.message)
        task = context.current_task
        run_id = _run_of(args, task)
        if skill in QUERIES or run_id is None:  # only reachable without `prepare`
            raise InvalidParamsError(message=f"{skill} is not run on a task")
        await self._advance(context, event_queue, task, run_id, _max_steps(args))

    def _check_run(self, run_id: str) -> None:
        try:
            exists = RunStore(self.service.run_dir(run_id)).exists()
        except RunStateError as exc:
            raise InvalidParamsError(message=str(exc)) from exc
        if not exists:
            raise InvalidParamsError(message=f"no run {run_id!r}")

    async def _call(self, fn: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        try:
            return await asyncio.to_thread(fn, *args, **kwargs)
        except InvalidParamsError:
            raise
        except Exception as exc:  # a refused request, not a server fault
            raise InvalidParamsError(message=f"{type(exc).__name__}: {exc}") from exc

    async def _advance(
        self,
        context: RequestContext,
        event_queue: EventQueue,
        task: Task | None,
        run_id: str,
        max_steps: int,
    ) -> None:
        if task is None:
            assert context.message is not None
            task = new_task_from_user_message(context.message)
            task.metadata.update({RUN_ID_KEY: run_id})
            await event_queue.enqueue_event(task)
        updater = TaskUpdater(event_queue, task.id, task.context_id)
        stop = self._stops[task.id] = threading.Event()
        try:
            seq = len(await asyncio.to_thread(self.service.events, run_id, 0))
            await updater.update_status(TaskState.TASK_STATE_WORKING)
            work = asyncio.ensure_future(
                asyncio.to_thread(self.service.advance, run_id, max_steps, stop)
            )
            try:
                while True:
                    done, _ = await asyncio.wait({work}, timeout=self.poll_seconds)
                    for event in await asyncio.to_thread(self.service.events, run_id, seq):
                        seq = event["seq"] + 1
                        message = new_message(
                            [new_text_part(_event_text(event)), new_data_part(_json(event))],
                            task.context_id,
                            task.id,
                        )
                        await updater.update_status(TaskState.TASK_STATE_WORKING, message)
                    if done:
                        break
            except asyncio.CancelledError:
                stop.set()  # the thread stops after its current step
                raise
            try:
                result = work.result()
            except Exception as exc:  # the run could not be opened or stepped: it stays as it is
                current = (await asyncio.to_thread(self.service.status, run_id))["current"]
                result = StepResult(Outcome.BLOCKED, current, f"{type(exc).__name__}: {exc}")
            state, why, stop = await asyncio.to_thread(self.service.task_state, run_id, result)
            evidence = None
            if (
                state == TaskState.TASK_STATE_INPUT_REQUIRED
                and result.outcome is Outcome.AWAITING_HUMAN
            ):
                try:
                    evidence = await asyncio.to_thread(self.service.evidence, run_id)
                    await updater.add_artifact([new_data_part(_json(evidence))], name="evidence")
                except Exception as exc:  # unverifiable evidence is reported, never invented
                    why = f"{why} Evidence could not be recomputed: {exc}"
            await updater.update_status(
                TaskState.TASK_STATE_WORKING,
                new_message(
                    [new_text_part("writing the summary of this stop")], task.context_id, task.id
                ),
            )
            try:
                summary = await asyncio.to_thread(self.service.stop_view, run_id, result, evidence)
                await updater.add_artifact([new_data_part(_json(summary))], name="summary")
            except Exception as exc:  # advisory: the stop is reported with or without it
                why = f"{why} (No summary was written: {type(exc).__name__}: {exc})"
            self.service.convert_in_background(run_id)
            await updater.update_status(
                state,
                new_message([new_text_part(why), new_data_part(stop)], task.context_id, task.id),
            )
        finally:
            self._stops.pop(task.id, None)

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        task_id = context.task_id or ""
        if (stop := self._stops.get(task_id)) is not None:
            stop.set()
        if context.current_task is None:
            return
        updater = TaskUpdater(event_queue, task_id, context.current_task.context_id)
        await updater.cancel(
            new_message(
                [
                    new_text_part(
                        "cancelled: the run stops after its current step and stays resumable"
                    )
                ],
                context.current_task.context_id,
                task_id,
            )
        )


class PipelineRequestHandler(DefaultRequestHandler):
    """The SDK's handler, with every request checked (and queries answered) before a task is
    touched: an executor that raises would leave its task FAILED, which is terminal."""

    def __init__(self, executor: PipelineExecutor, store: RunTaskStore, card: AgentCard) -> None:
        super().__init__(agent_executor=executor, task_store=store, agent_card=card)
        self._pipeline = executor
        self._tasks = store

    async def _prepare(
        self, params: SendMessageRequest, context: ServerCallContext
    ) -> Message | None:
        message = params.message
        task = None
        if message.task_id:
            task = await self._tasks.get(message.task_id, context)
            if task is None:
                raise TaskNotFoundError(f"Task {message.task_id} not found")
            if not message.context_id:
                message.context_id = task.context_id  # the task's own context, as A2A infers it
        return await self._pipeline.prepare(message, task)

    async def on_message_send(
        self, params: SendMessageRequest, context: ServerCallContext
    ) -> Message | Task:
        reply = await self._prepare(params, context)
        if reply is not None:
            return reply
        return await super().on_message_send(params, context)

    async def on_message_send_stream(  # type: ignore[override]
        self, params: SendMessageRequest, context: ServerCallContext
    ) -> AsyncGenerator[Any]:
        reply = await self._prepare(params, context)
        if reply is not None:
            yield reply
            return
        async for event in super().on_message_send_stream(params, context):
            yield event


def _json(value: Any) -> Any:
    """JSON-safe copy (paths and other values become strings)."""
    return json.loads(json.dumps(value, default=str))


# ── HTTP: card, token gate, loopback socket ─────────────────────────────────


def agent_card(url: str) -> AgentCard:
    try:
        version = metadata.version("agentic-pipeline")
    except metadata.PackageNotFoundError:
        version = "0"
    return AgentCard(
        name=AGENT_NAME,
        description=(
            "The ln-agents delivery pipeline. A deterministic orchestrator owns every "
            "transition; humans sign approvals outside A2A."
        ),
        version=version,
        supported_interfaces=[
            AgentInterface(url=url, protocol_binding="JSONRPC", protocol_version="1.0")
        ],
        capabilities=AgentCapabilities(streaming=True, push_notifications=False),
        security_schemes={
            "bearer": SecurityScheme(
                http_auth_security_scheme=HTTPAuthSecurityScheme(scheme="bearer")
            )
        },
        security_requirements=[SecurityRequirement(schemes={"bearer": StringList(list=[])})],
        default_input_modes=["application/json"],
        default_output_modes=["application/json", "text/plain"],
        skills=[
            AgentSkill(id=skill, name=skill, description=SKILL_TEXT[skill], tags=["ln-pipeline"])
            for skill in SKILLS
        ],
    )


type Asgi = Callable[
    [dict[str, Any], Callable[[], Awaitable[Any]], Callable[[Any], Awaitable[None]]],
    Awaitable[None],
]


class BearerGate:
    """Deny by default: only GET/HEAD of the agent card is public."""

    def __init__(self, app: Asgi, token: str) -> None:
        self.app = app
        self.expected = f"Bearer {token}".encode()

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        public = scope["method"] in ("GET", "HEAD") and scope["path"] == AGENT_CARD_WELL_KNOWN_PATH
        given = next((v for k, v in scope["headers"] if k == b"authorization"), b"")
        if public or hmac.compare_digest(given, self.expected):
            await self.app(scope, receive, send)
            return
        await JSONResponse({"error": "missing or wrong bearer token"}, status_code=401)(
            scope, receive, send
        )


def build_app(
    service: PipelineService, token: str, url: str, poll_seconds: float = POLL_SECONDS
) -> tuple[Asgi, PipelineExecutor]:
    executor = PipelineExecutor(service, poll_seconds)
    store = RunTaskStore(service.runs_dir / TASKS_DIR / "tasks", service, executor.is_live)
    card = agent_card(url)
    handler = PipelineRequestHandler(executor, store, card)
    app = Starlette(
        routes=[*create_agent_card_routes(card), *create_jsonrpc_routes(handler, RPC_PATH)]
    )
    return BearerGate(app, token), executor


def loopback(host: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    """The bind address, which must be a literal loopback address (no hostnames)."""
    try:
        address = ipaddress.ip_address(host)
    except ValueError as exc:
        raise ServeError(f"{host!r} is not a literal IP address; use 127.0.0.1 or ::1") from exc
    if not address.is_loopback:
        raise ServeError(f"{host} is not a loopback address; the server binds loopback only")
    return address


def bind(host: str, port: int) -> socket.socket:
    """Listen on a loopback address. No SO_REUSEADDR: on Windows it would let another process
    take the port."""
    address = loopback(host)
    family = socket.AF_INET6 if address.version == 6 else socket.AF_INET
    sock = socket.socket(family, socket.SOCK_STREAM)
    try:
        sock.bind((str(address), port))
        sock.listen()
    except OSError:
        sock.close()
        raise
    return sock


def load_token(path: Path) -> str:
    """The bearer token: created (0600) if missing; refused if others may read it."""
    if not path.exists():
        path.parent.mkdir(parents=True, exist_ok=True)
        token = secrets.token_urlsafe(32)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(token + "\n")
        return token
    if not WINDOWS and path.stat().st_mode & 0o077:
        raise ServeError(f"{path} is readable by others; chmod 600 it")
    token = path.read_text("utf-8").strip()
    if len(token) < 32:
        raise ServeError(f"{path} holds no usable token (32+ characters)")
    return token


def serve(root: Path, runs_dir: Path, host: str, port: int, token_file: Path) -> None:
    token = load_token(token_file)
    sock = bind(host, port)
    address, bound = sock.getsockname()[:2]
    shown = f"[{address}]" if ":" in address else address
    url = f"http://{shown}:{bound}{RPC_PATH}"
    app, _ = build_app(PipelineService(root, runs_dir), token, url)
    print(
        f"{AGENT_NAME} A2A server on {url} (card: http://{shown}:{bound}{AGENT_CARD_WELL_KNOWN_PATH})"
    )
    print(f"bearer token in {token_file}", flush=True)
    uvicorn.Server(uvicorn.Config(app, log_level="warning")).run(sockets=[sock])
