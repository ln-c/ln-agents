"""The A2A server: card, token gate, skills, task states, durable tasks, loopback binding.

Runs use fake adapters and handlers (no model is called); HTTP goes through Starlette's
in-process TestClient, so no socket is opened except in the explicit bind tests.
"""

from __future__ import annotations

import ast
import asyncio
import json
import os
import socket
import subprocess
import sys
import threading
import warnings
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

import pytest
from test_dispatch import DATA_CLASSES, GRAPH, REGISTRY, FakeAdapter, report, seed

pytest.importorskip("a2a")
from a2a.server.context import ServerCallContext  # noqa: E402
from a2a.types.a2a_pb2 import Task, TaskState, TaskStatus  # noqa: E402

with warnings.catch_warnings():  # Starlette asks for httpx2; httpx is what a2a-sdk depends on
    warnings.simplefilter("ignore")
    from starlette.testclient import TestClient

from agentic_pipeline import a2a_server  # noqa: E402
from agentic_pipeline.a2a_server import (  # noqa: E402
    RUN_ID_KEY,
    SKILLS,
    PipelineService,
    RunTaskStore,
    ServeError,
    bind,
    build_app,
    load_token,
    loopback,
)
from agentic_pipeline.dispatch import Dispatcher  # noqa: E402
from agentic_pipeline.run_state import RunStore, Status  # noqa: E402
from agentic_pipeline.runner import StateHandler, init_run  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
RUN_ID = "run-1"
TOKEN = "t" * 43
AUTH = {"Authorization": f"Bearer {TOKEN}", "A2A-Version": "1.0"}
ALWAYS: StateHandler = lambda ctx: "always"  # noqa: E731
# START -> G -> A1 .. A3 (fake models) -> A4 -> A3R -> B1 -> H1: a run that ends awaiting a human.
TO_H1: dict[str, StateHandler] = {
    "START": lambda ctx: "task_class_valid",
    "G": ALWAYS,
    "A2": ALWAYS,
    "A2J": ALWAYS,
    "A4": lambda ctx: "all_required_valid",
    "B1": lambda ctx: "all_vetoed_otherwise",
}


class Pipeline:
    """A run under tmp_path, the service over it, and an in-process client of the app."""

    def __init__(self, tmp_path: Path, handlers: Mapping[str, StateHandler] = TO_H1) -> None:
        self.runs_dir = tmp_path
        self.run_dir = tmp_path / RUN_ID
        backends = ("claude_cli_default", "gemini_3_8_flash_high", "qwen3_5_9b", "clef_flash_local")
        self.adapters = {b: FakeAdapter() for b in backends}
        seed(self.run_dir, ("START", "G"))
        init_run(ROOT, self.run_dir, RUN_ID, "START")
        self.service = PipelineService(ROOT, tmp_path, self._factory, handlers)
        app, self.executor = build_app(self.service, TOKEN, "http://127.0.0.1:0/a2a/jsonrpc", 0.01)
        self.client = TestClient(app)

    def _factory(self, root: Path, run_dir: Path) -> Any:
        def make(policy: Any, log: Any, ledger: Any, guards: Any) -> Dispatcher:
            return Dispatcher(
                graph=GRAPH,
                registry=REGISTRY,
                log=log,
                ledger=ledger,
                adapters=self.adapters,
                preflight=report,
                run_root=run_dir,
                data_classes=DATA_CLASSES,
                guards=guards,
            )

        return make

    def rpc(self, method: str, params: Mapping[str, Any], headers: Mapping[str, str] = AUTH) -> Any:
        body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        return self.client.post("/a2a/jsonrpc", headers=dict(headers), json=body)

    def send(self, data: Mapping[str, Any], **ids: str) -> dict[str, Any]:
        message = {"messageId": "m", "role": "ROLE_USER", "parts": [{"data": dict(data)}], **ids}
        reply = self.rpc("SendMessage", {"message": message})
        assert reply.status_code == 200, reply.text
        return reply.json()

    def stream(self, data: Mapping[str, Any]) -> list[dict[str, Any]]:
        message = {"messageId": "m", "role": "ROLE_USER", "parts": [{"data": dict(data)}]}
        body = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "SendStreamingMessage",
            "params": {"message": message},
        }
        events = []
        with self.client.stream("POST", "/a2a/jsonrpc", headers=AUTH, json=body) as reply:
            for line in reply.iter_lines():
                if line.startswith("data:"):
                    events.append(json.loads(line[5:])["result"])
        return events


@pytest.fixture
def pipe(tmp_path: Path) -> Iterator[Pipeline]:
    pipeline = Pipeline(tmp_path)
    yield pipeline
    pipeline.client.close()


def _state(task_or_event: Mapping[str, Any]) -> str:
    return task_or_event["status"]["state"]


def _text(task_or_event: Mapping[str, Any]) -> str:
    parts = task_or_event["status"].get("message", {}).get("parts", [])
    return " ".join(p["text"] for p in parts if p.get("text"))


# ── Card and token gate ──────────────────────────────────────────────────────


def test_the_card_is_public_and_names_exactly_the_five_skills(pipe: Pipeline) -> None:
    card = pipe.client.get("/.well-known/agent-card.json").json()
    assert card["name"] == "ln-pipeline"
    assert sorted(s["id"] for s in card["skills"]) == sorted(SKILLS)
    assert set(SKILLS) == {"start_run", "advance", "status", "evidence", "approval_document"}
    assert card["securitySchemes"]["bearer"]["httpAuthSecurityScheme"]["scheme"] == "bearer"
    assert card["capabilities"]["streaming"] is True


@pytest.mark.parametrize(
    ("method", "path", "headers", "expected"),
    [
        ("POST", "/a2a/jsonrpc", {}, 401),
        ("POST", "/a2a/jsonrpc", {"Authorization": "Bearer wrong"}, 401),
        ("POST", "/.well-known/agent-card.json", {}, 401),  # only GET/HEAD of the card is public
        ("GET", "/a2a/rest/tasks", {}, 401),  # a route nobody registered is still gated
        ("GET", "/anything", {"Authorization": f"Bearer {TOKEN}"}, 404),
    ],
)
def test_everything_but_the_card_needs_the_token(
    pipe: Pipeline, method: str, path: str, headers: dict[str, str], expected: int
) -> None:
    assert pipe.client.request(method, path, headers=headers).status_code == expected


# ── Skills ───────────────────────────────────────────────────────────────────


def test_status_without_a_task_answers_with_a_message(pipe: Pipeline) -> None:
    reply = pipe.send({"skill": "status", "run_id": RUN_ID})
    (part,) = reply["result"]["message"]["parts"]
    assert part["data"]["skill"] == "status"
    assert part["data"]["current"] == "START" and part["data"]["runner_active"] is False


@pytest.mark.parametrize(
    ("data", "error"),
    [
        ({"skill": "decide", "run_id": RUN_ID}, "unknown skill 'decide'"),
        ({"skill": "resume", "run_id": RUN_ID}, "unknown skill 'resume'"),
        ({"skill": "status", "run_id": RUN_ID, "approve": True}, "does not take ['approve']"),
        ({"skill": "approval_document", "run_id": RUN_ID}, "needs ['approver', 'decision'"),
        ({"skill": "status", "run_id": "nope"}, "no run 'nope'"),
        ({"skill": "status", "run_id": "../escape"}, "run id"),
        ({"skill": "advance", "run_id": RUN_ID, "max_steps": 0}, "positive integer"),
        ({"run_id": RUN_ID}, "unknown skill None"),
    ],
)
def test_refused_requests_change_nothing(pipe: Pipeline, data: dict, error: str) -> None:
    before = (pipe.run_dir / "state.json").read_bytes()
    reply = pipe.send(data)
    assert reply["error"]["code"] == -32602 and error in reply["error"]["message"], reply
    assert (pipe.run_dir / "state.json").read_bytes() == before


def test_a_text_part_is_not_a_request(pipe: Pipeline) -> None:
    message = {"messageId": "m", "role": "ROLE_USER", "parts": [{"text": "approve it please"}]}
    reply = pipe.rpc("SendMessage", {"message": message}).json()
    assert reply["error"]["code"] == -32602


def test_advance_streams_every_step_and_stops_for_a_human(pipe: Pipeline) -> None:
    events = pipe.stream({"skill": "advance", "run_id": RUN_ID})
    updates = [e["statusUpdate"] for e in events if "statusUpdate" in e]
    texts = [_text(u) for u in updates]
    assert "entered G" in texts and "entered H1" in texts
    assert any(t.startswith("A1.proposal_a: calling") for t in texts)
    final = updates[-1]
    assert _state(final) == "TASK_STATE_INPUT_REQUIRED"
    assert "signed approval at H1" in _text(final) and "cannot approve" in _text(final)
    # The run lock is free while the task waits, so a human can run `run decide`.
    store = RunStore(pipe.run_dir)
    store.acquire(wait=0)
    store.release()
    assert store.load().status is Status.AWAITING_HUMAN


def test_a_task_waiting_for_a_human_continues_on_the_same_task(pipe: Pipeline) -> None:
    first = pipe.send({"skill": "advance", "run_id": RUN_ID})["result"]["task"]
    assert _state(first) == "TASK_STATE_INPUT_REQUIRED"
    assert first["metadata"][RUN_ID_KEY] == RUN_ID
    ids = {"taskId": first["id"], "contextId": first["contextId"]}
    again = pipe.send({"skill": "status"}, **ids)["result"]["task"]  # run_id from the task
    assert _state(again) == "TASK_STATE_INPUT_REQUIRED"
    assert again["status"]["message"]["parts"][0]["data"]["current"] == "H1"


def test_start_run_creates_the_run_then_advances_it(tmp_path: Path) -> None:
    pipe = Pipeline(tmp_path)
    from test_grounding import SPEC as spec

    seed(tmp_path / "run-2", ("START", "G"))
    events = pipe.stream({"skill": "start_run", "run_id": "run-2", "entry_spec": spec})
    assert "task" in events[0] and events[0]["task"]["metadata"][RUN_ID_KEY] == "run-2"
    assert RunStore(tmp_path / "run-2").exists()
    assert pipe.send({"skill": "start_run", "run_id": "run-2", "entry_spec": spec})["error"]


def test_a_quota_pause_asks_for_auth(tmp_path: Path) -> None:
    pipe = Pipeline(tmp_path)
    pipe.adapters["claude_cli_default"] = FakeAdapter(status="quota_exhausted")
    task = pipe.send({"skill": "advance", "run_id": RUN_ID})["result"]["task"]
    assert _state(task) == "TASK_STATE_AUTH_REQUIRED"
    assert "quota_exhausted" in _text(task) and "run resume" in _text(task)


def test_a_blocked_run_stays_resumable_not_failed(tmp_path: Path) -> None:
    pipe = Pipeline(tmp_path, handlers={"START": lambda ctx: "task_class_valid"})  # no G
    task = pipe.send({"skill": "advance", "run_id": RUN_ID})["result"]["task"]
    assert _state(task) == "TASK_STATE_INPUT_REQUIRED"
    assert "no handler registered for G" in _text(task) and "advance" in _text(task)


def test_evidence_outside_a_human_state_is_refused(pipe: Pipeline) -> None:
    reply = pipe.send({"skill": "evidence", "run_id": RUN_ID})
    assert reply["error"]["code"] == -32602 and "awaiting_human" in reply["error"]["message"]


# ── Durable tasks and cancellation ───────────────────────────────────────────


def test_a_stored_working_task_is_answered_from_the_run_after_a_restart(pipe: Pipeline) -> None:
    store = RunTaskStore(pipe.runs_dir / ".a2a" / "tasks", pipe.service, lambda _: True)
    task = Task(id="t1", context_id="c1", status=TaskStatus(state=TaskState.TASK_STATE_WORKING))
    task.metadata.update({RUN_ID_KEY: RUN_ID})
    context = ServerCallContext()
    asyncio.run(store.save(task, context))
    # A new server process: nothing is driving t1 any more.
    fresh = RunTaskStore(pipe.runs_dir / ".a2a" / "tasks", pipe.service, lambda _: False)
    found = asyncio.run(fresh.get("t1", context))
    assert found is not None and found.status.state == TaskState.TASK_STATE_INPUT_REQUIRED
    assert "stopped at START" in found.status.message.parts[0].text
    listed = asyncio.run(fresh.list(a2a_server.ListTasksRequest(), context))
    assert [t.status.state for t in listed.tasks] == [TaskState.TASK_STATE_INPUT_REQUIRED]


def test_a_cancelled_advance_stops_before_its_next_step_and_frees_the_lock(pipe: Pipeline) -> None:
    stop = threading.Event()
    stop.set()
    result = pipe.service.advance(RUN_ID, 100, stop)
    assert result.detail == "cancelled" and result.node == "START"
    RunStore(pipe.run_dir).acquire(wait=0)


# ── Nothing here can approve ─────────────────────────────────────────────────


def test_the_server_module_never_reaches_a_decision_path() -> None:
    tree = ast.parse(Path(a2a_server.__file__).read_text(encoding="utf-8"))
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    names |= {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    names |= {a.name for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) for a in n.names}
    forbidden = {"decide", "verify_approval", "store_approval", "resume", "parse_signed"}
    assert not names & forbidden, names & forbidden


# ── Binding and token ────────────────────────────────────────────────────────


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.4", "::", "localhost", "example.com"])  # noqa: S104
def test_only_a_literal_loopback_address_is_accepted(host: str) -> None:
    with pytest.raises(ServeError):
        loopback(host)


def test_bind_listens_on_loopback_only() -> None:
    sock = bind("127.0.0.1", 0)
    try:
        assert sock.getsockname()[0] == "127.0.0.1"
        assert sock.getsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR) == 0
    finally:
        sock.close()


def test_the_token_file_is_created_private_and_refused_when_shared(tmp_path: Path) -> None:
    path = tmp_path / ".a2a" / "token"
    token = load_token(path)
    assert len(token) >= 32 and load_token(path) == token
    if os.name != "nt":
        assert path.stat().st_mode & 0o777 == 0o600
        path.chmod(0o644)
        with pytest.raises(ServeError, match="readable by others"):
            load_token(path)
        path.chmod(0o600)
    path.write_text("short\n")
    with pytest.raises(ServeError, match="no usable token"):
        load_token(path)


# ── The core install works without the extra ─────────────────────────────────


def _without_a2a(*argv: str) -> subprocess.CompletedProcess[str]:
    code = (
        "import sys\n"
        "for name in ('a2a', 'uvicorn', 'starlette'):\n"
        "    sys.modules[name] = None\n"
        "from agentic_pipeline.cli import main\n"
        f"sys.exit(main({list(argv)!r}))\n"
    )
    env = {**os.environ, "PYTHONPATH": str(ROOT / "src")}
    return subprocess.run(  # noqa: S603 - fixed argv
        [sys.executable, "-c", code], capture_output=True, text=True, env=env, check=False
    )


def test_validate_runs_without_the_a2a_extra() -> None:
    done = _without_a2a("validate", "--root", str(ROOT))
    assert done.returncode == 0, done.stderr


def test_serve_without_the_extra_says_what_to_install(tmp_path: Path) -> None:
    done = _without_a2a("serve", "a2a", "--runs-dir", str(tmp_path))
    assert done.returncode == 1 and "uv sync --extra a2a" in done.stderr
