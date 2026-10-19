"""The A2A server: card, token gate, skills, task states, durable tasks, loopback binding.

Runs use fake adapters and handlers (no model is called); HTTP goes through Starlette's
in-process TestClient, so no socket is opened except in the explicit bind tests.
"""

from __future__ import annotations

import ast
import asyncio
import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
import warnings
from collections.abc import Iterator, Mapping
from pathlib import Path
from typing import Any

import pytest
from test_coordinator import FakeModel
from test_dispatch import BINDABLE, DATA_CLASSES, GRAPH, REGISTRY, A, FakeAdapter, report, seed

pytest.importorskip("a2a")
from a2a.server.context import ServerCallContext  # noqa: E402
from a2a.types.a2a_pb2 import Task, TaskState, TaskStatus  # noqa: E402

with warnings.catch_warnings():  # Starlette asks for httpx2; httpx is what a2a-sdk depends on
    warnings.simplefilter("ignore")
    from starlette.testclient import TestClient

from agentic_pipeline import a2a_server  # noqa: E402
from agentic_pipeline import registry_changes as rc  # noqa: E402
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
from agentic_pipeline.coordinator import Coordinator, Setup  # noqa: E402
from agentic_pipeline.dispatch import Dispatcher  # noqa: E402
from agentic_pipeline.graph import loads_yaml  # noqa: E402
from agentic_pipeline.models_cli import CatalogError, Deps, catalog_rows  # noqa: E402
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
        self.adapters = {b: FakeAdapter() for b in BINDABLE}
        seed(self.run_dir, ("START", "G"))
        init_run(ROOT, self.run_dir, RUN_ID, "START")
        self.calls: list[str] = []
        self.service = PipelineService(
            ROOT, tmp_path, self._factory, handlers, coordinator_for=self._coordinator
        )
        app, self.executor = build_app(self.service, TOKEN, "http://127.0.0.1:0/a2a/jsonrpc", 0.01)
        self.client = TestClient(app)

    reply: Any = staticmethod(lambda prompt: "A summary written by the fake coordinator.")
    setup: Setup | None = Setup("gemini_3_8_flash_high", None, "", ROOT / "LN-AGENTS.md")

    def _coordinator(self, root: Path, run_dir: Path) -> Coordinator:
        """The run's coordinator, played by a fake model (or absent when `setup` is None)."""
        return Coordinator(
            root=root,
            run_dir=run_dir,
            run_id=run_dir.name,
            registry=REGISTRY,
            data_classes=DATA_CLASSES,
            adapter_factory=lambda registry, backend, **kw: FakeModel(
                kw["task_builder"], self.calls, self.reply
            ),
            preflight=report,
            setup=self.setup,
        )

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


def _stop(task_or_event: Mapping[str, Any]) -> dict[str, Any]:
    """The {run_status, node} data part of a stopped task's status message."""
    parts = task_or_event["status"]["message"]["parts"]
    return next(p["data"] for p in parts if "data" in p)


# ── Card and token gate ──────────────────────────────────────────────────────


def test_the_card_is_public_and_names_exactly_the_thirteen_skills(pipe: Pipeline) -> None:
    card = pipe.client.get("/.well-known/agent-card.json").json()
    assert card["name"] == "ln-pipeline"
    assert sorted(s["id"] for s in card["skills"]) == sorted(SKILLS)
    assert set(SKILLS) == {
        "start_run",
        "advance",
        "status",
        "evidence",
        "approval_document",
        "ask",
        "models",
        "agents",
        "registry_change",
        "propose_mapping",
        "complete_mapping",
        "apply_change",
        "discard_change",
    }
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
    assert _stop(final) == {"run_status": "awaiting_human", "node": "H1"}
    # The run lock is free while the task waits, so a human can run `run decide`.
    store = RunStore(pipe.run_dir)
    store.acquire(wait=0)
    store.release()
    assert store.load().status is Status.AWAITING_HUMAN


def _artifacts(events: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    found = {}
    for e in events:
        update = e.get("artifactUpdate")
        if update:
            found[update["artifact"]["name"]] = update["artifact"]["parts"][0]["data"]
    return found


def test_a_stop_comes_with_the_coordinators_summary_written_to_the_runs_views(
    pipe: Pipeline,
) -> None:
    events = pipe.stream({"skill": "advance", "run_id": RUN_ID})
    summary = _artifacts(events)["summary"]
    assert summary["kind"] == "awaiting_human"
    assert "A summary written by the fake coordinator." in summary["markdown"]
    assert "## What to do next" in summary["markdown"]  # the commands are code's, not the model's
    path = Path(summary["path"])
    assert path == (pipe.run_dir / "views" / "STOP-001-H1.md").resolve() and path.is_file()
    assert _state(events[-1]["statusUpdate"]) == "TASK_STATE_INPUT_REQUIRED"


def test_a_stop_is_reported_even_when_there_is_no_coordinator(tmp_path: Path) -> None:
    pipe = Pipeline(tmp_path)
    pipe.setup = None
    events = pipe.stream({"skill": "advance", "run_id": RUN_ID})
    summary = _artifacts(events)["summary"]
    assert "No coordinator summary" in summary["markdown"]
    assert _state(events[-1]["statusUpdate"]) == "TASK_STATE_INPUT_REQUIRED"


def test_only_one_conversion_runs_per_run_and_it_ends(pipe: Pipeline) -> None:
    pipe.send({"skill": "advance", "run_id": RUN_ID})  # a stop starts one in the background
    deadline = time.monotonic() + 30
    while pipe.service._converting and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not pipe.service._converting
    started = pipe.service.convert_in_background(RUN_ID)
    assert started is not None
    started.join(30)
    assert not started.is_alive() and not pipe.service._converting


def test_a_question_is_answered_by_message_and_changes_no_task(pipe: Pipeline) -> None:
    first = pipe.send({"skill": "advance", "run_id": RUN_ID})["result"]["task"]
    reply = pipe.send({"skill": "ask", "question": "why did it stop?"}, taskId=first["id"])
    message = reply["result"]["message"]
    assert message["parts"][0]["data"]["answer"] == "A summary written by the fake coordinator."
    assert "STOP-001-H1.md" in message["parts"][0]["data"]["sources"]
    task = pipe.rpc("GetTask", {"id": first["id"]}).json()["result"]
    assert task["status"] == first["status"]
    assert any("why did it stop?" in call for call in pipe.calls)  # conversions share the list


def test_models_is_answered_by_message_without_a_run_or_a_task(pipe: Pipeline) -> None:
    rows = [{"name": "qwen3_5_9b", "bound_to": ["E1.implementation_patch"]}]
    seen: list[Path] = []
    pipe.service.catalog = lambda root: seen.append(root) or rows
    reply = pipe.send({"skill": "models"})
    data = reply["result"]["message"]["parts"][0]["data"]
    assert data["skill"] == "models" and data["models"] == rows and data["errors"] == []
    assert seen == [ROOT]
    assert pipe.rpc("ListTasks", {}).json()["result"].get("tasks", []) == []  # no task was made


def test_models_reports_a_repository_that_fails_validation_instead_of_a_catalog(
    pipe: Pipeline,
) -> None:
    def broken(root: Path) -> list[dict[str, Any]]:
        raise CatalogError(["registry R1: broken"])

    pipe.service.catalog = broken
    data = pipe.send({"skill": "models"})["result"]["message"]["parts"][0]["data"]
    assert data["models"] == [] and data["errors"] == ["registry R1: broken"]


def test_models_takes_no_arguments_and_changes_nothing(pipe: Pipeline) -> None:
    reply = pipe.send({"skill": "models", "run_id": RUN_ID})
    assert reply["error"]["code"] == -32602 and "does not take ['run_id']" in str(reply["error"])


def registry_repo(pipe: Pipeline, tmp_path: Path) -> Path:
    """Point the service at a copy of the repository, so `apply_change` writes a copy."""
    root = tmp_path / "repo"
    for name in ("pipeline", "policy", "schemas", "agents"):
        shutil.copytree(ROOT / name, root / name)
    shutil.copy(ROOT / "LN-AGENTS.md", root / "LN-AGENTS.md")
    pipe.service.root = root
    return root


def data_of(reply: dict[str, Any]) -> dict[str, Any]:
    return reply["result"]["message"]["parts"][0]["data"]


ADD = {"skill": "registry_change", "kind": "add_model", "provider": "google_ai_pro"}


def test_a_registry_change_is_drafted_then_applied_by_id_and_makes_no_task(
    pipe: Pipeline, tmp_path: Path
) -> None:
    root = registry_repo(pipe, tmp_path)
    before = (root / "policy" / "model_registry.yaml").read_text()
    draft = data_of(
        pipe.send({**ADD, "model_id": "gemini-9-new", "capabilities": ["design_reasoning"]})
    )
    assert draft["errors"] == [] and draft["kind"] == "add_model" and len(draft["change_id"]) == 8
    assert "+  gemini_9_new:" in draft["diff"] and draft["summary"].startswith(
        "Add model gemini_9_new"
    )
    assert (root / "policy" / "model_registry.yaml").read_text() == before, "a draft writes nothing"
    pending = data_of(pipe.send({"skill": "agents"}))["pending"]
    assert [p["id"] for p in pending] == [draft["change_id"]]

    applied = data_of(pipe.send({"skill": "apply_change", "change_id": draft["change_id"]}))
    assert applied["errors"] == [] and applied["applied"]["path"] == "policy/model_registry.yaml"
    assert (
        "gemini_9_new: {provider: google_ai_pro"
        in (root / "policy" / "model_registry.yaml").read_text()
    )
    again = data_of(pipe.send({"skill": "apply_change", "change_id": draft["change_id"]}))
    assert again["applied"] is None and "no pending change" in again["errors"][0]
    assert pipe.rpc("ListTasks", {}).json()["result"].get("tasks", []) == []


def test_a_refused_change_comes_back_as_errors_with_no_draft(
    pipe: Pipeline, tmp_path: Path
) -> None:
    registry_repo(pipe, tmp_path)
    reply = data_of(pipe.send({**ADD, "model_id": "mystery-1"}))
    assert reply["change_id"] is None and "no family pattern" in reply["errors"][0]
    reply = data_of(
        pipe.send(
            {
                "skill": "registry_change",
                "kind": "bind",
                "slot": "design.proposal_b.primary",
                "model": "claude_cli_default",
            }
        )
    )
    assert reply["change_id"] is None and reply["errors"][0].startswith("registry R14")
    assert data_of(pipe.send({"skill": "agents"}))["pending"] == []


@pytest.mark.parametrize(
    ("data", "problem"),
    [
        ({"kind": "frobnicate"}, "unknown change 'frobnicate'"),
        ({"kind": "add_model", "provider": "google_ai_pro"}, "add_model needs model_id"),
        (
            {"kind": "add_model", "provider": "google_ai_pro", "model_id": "g", "slot": "x"},
            "does not take ['slot']",
        ),
        ({"kind": "remove_model"}, "remove_model needs model"),
        ({"kind": "bind", "slot": "build.primary"}, "name one to eight distinct models"),
        (
            {
                "kind": "add_model",
                "provider": "google_ai_pro",
                "model_id": "g",
                "capabilities": "x",
            },
            "must be a list of strings",
        ),
    ],
)
def test_a_malformed_registry_change_is_reported_not_drafted(
    pipe: Pipeline, tmp_path: Path, data: dict[str, Any], problem: str
) -> None:
    registry_repo(pipe, tmp_path)
    reply = data_of(pipe.send({"skill": "registry_change", **data}))
    assert reply["change_id"] is None and problem in reply["errors"][0], reply


PROPOSAL = {
    "bindings": [
        {
            "slot": "design.proposal_b.primary",
            "models": ["gpt_oss_120b_medium"],
            "why": "A third family keeps the designs independent.",
        }
    ],
    "summary": "Design B moves to GPT-OSS.",
}


def authority_pipe(pipe: Pipeline, tmp_path: Path, reply: Any) -> Path:
    """A service whose authority is the fake model, over a copy of the repository."""
    root = registry_repo(pipe, tmp_path)
    pipe.reply = lambda prompt: reply if isinstance(reply, str) else json.dumps(reply)

    def authority_for(root_: Path, runs: Path) -> Coordinator:
        coordinator = pipe._coordinator(root_, runs / ".authority")
        coordinator.run_id = "authority"  # the directory's own name is not a valid run id
        return coordinator

    pipe.service.authority_for = authority_for

    def catalog(root_: Path) -> list[dict[str, Any]]:
        models = loads_yaml((root_ / "policy" / "model_registry.yaml").read_bytes())["models"]
        return [
            {
                "name": name,
                "family": m["family"],
                "location": "cloud",
                "capabilities": m["capabilities"],
                "bound_to": [],
                "readiness": None,
                "conformance": None,
            }
            for name, m in models.items()
        ]

    pipe.service.catalog = catalog
    return root


def test_the_authoritys_proposal_is_a_draft_and_writes_nothing(
    pipe: Pipeline, tmp_path: Path
) -> None:
    root = authority_pipe(pipe, tmp_path, PROPOSAL)
    before = (root / "policy" / "model_registry.yaml").read_text()
    data = data_of(pipe.send({"skill": "propose_mapping", "intent": "use a third family"}))
    assert data["errors"] == [] and data["kind"] == "bind_many" and len(data["change_id"]) == 8
    assert data["proposed_by"] == "gemini_3_8_flash_high"
    assert data["authority_says"] == "Design B moves to GPT-OSS."
    assert "+      primary: gpt_oss_120b_medium" in data["diff"]
    assert (root / "policy" / "model_registry.yaml").read_text() == before
    assert any("use a third family" in call for call in pipe.calls)
    assert [p["id"] for p in data_of(pipe.send({"skill": "agents"}))["pending"]] == [
        data["change_id"]
    ]
    applied = data_of(pipe.send({"skill": "apply_change", "change_id": data["change_id"]}))
    assert (
        applied["errors"] == []
        and "gpt_oss_120b_medium" in (root / "policy" / "model_registry.yaml").read_text()
    )
    assert pipe.rpc("ListTasks", {}).json()["result"].get("tasks", []) == []


def test_a_proposal_that_breaks_a_rule_comes_back_as_errors_with_no_draft(
    pipe: Pipeline, tmp_path: Path
) -> None:
    bad = {"bindings": [{"slot": "design.proposal_b.primary", "models": ["claude_cli_default"]}]}
    authority_pipe(pipe, tmp_path, bad)
    data = data_of(pipe.send({"skill": "propose_mapping"}))
    assert data["change_id"] is None and data["errors"][0].startswith("registry R14")
    assert data_of(pipe.send({"skill": "agents"}))["pending"] == []


def test_propose_mapping_without_a_usable_authority_says_why(
    pipe: Pipeline, tmp_path: Path
) -> None:
    authority_pipe(pipe, tmp_path, PROPOSAL)
    pipe.setup = None
    data = data_of(pipe.send({"skill": "propose_mapping"}))
    assert data["change_id"] is None and "names a coordinator" in data["errors"][0]


def test_propose_mapping_takes_only_an_intent(pipe: Pipeline) -> None:
    reply = pipe.send({"skill": "propose_mapping", "slot": "build.primary"})
    assert reply["error"]["code"] == -32602 and "does not take ['slot']" in str(reply["error"])
    data = data_of(pipe.send({"skill": "propose_mapping", "intent": 5}))
    assert data["change_id"] is None and data["errors"] == ["intent must be text"]


def test_apply_change_names_a_draft_and_can_carry_no_text(pipe: Pipeline) -> None:
    for extra in ({"text": "x"}, {"new_text": "x"}, {"path": "policy/budgets.yaml"}):
        reply = pipe.send({"skill": "apply_change", "change_id": "deadbeef", **extra})
        assert reply["error"]["code"] == -32602 and "does not take" in str(reply["error"]), extra
    reply = pipe.send({"skill": "apply_change"})
    assert reply["error"]["code"] == -32602 and "needs ['change_id']" in str(reply["error"])


def test_a_registry_change_cannot_name_another_file_or_a_run(pipe: Pipeline) -> None:
    for extra in ({"file": "policy/budgets.yaml"}, {"run_id": RUN_ID}, {"text": "x"}):
        reply = pipe.send({"skill": "registry_change", "kind": "bind", **extra})
        assert reply["error"]["code"] == -32602 and "does not take" in str(reply["error"]), extra


def test_a_draft_is_discarded_by_id_and_nothing_is_written(pipe: Pipeline, tmp_path: Path) -> None:
    root = registry_repo(pipe, tmp_path)
    before = (root / "policy" / "model_registry.yaml").read_text()
    draft = data_of(pipe.send({**ADD, "model_id": "gemini-9-new"}))
    gone = data_of(pipe.send({"skill": "discard_change", "change_id": draft["change_id"]}))
    assert gone["discarded"] == draft["summary"] and gone["errors"] == []
    assert data_of(pipe.send({"skill": "agents"}))["pending"] == []
    assert (root / "policy" / "model_registry.yaml").read_text() == before
    assert (
        data_of(pipe.send({"skill": "discard_change", "change_id": draft["change_id"]}))[
            "discarded"
        ]
        is None
    )


def test_agents_lists_the_slots_and_what_runs_on_them(pipe: Pipeline, tmp_path: Path) -> None:
    registry_repo(pipe, tmp_path)
    reply = data_of(pipe.send({"skill": "agents"}))
    slots = {s["slot"]: s for s in reply["slots"]}
    assert slots["design.proposal_b.primary"]["value"] == "gemini_4_argon_medium"
    assert slots["design.proposal_b.primary"]["operations"] == [
        "A1.proposal_b",
        "A2.critique_a_by_b",
    ]
    assert slots["design.synthesis.primary"]["rebindable"] is False
    assert reply["errors"] == [] and reply["pending"] == []


def test_a_question_without_a_coordinator_is_refused_and_leaves_the_run_alone(
    tmp_path: Path,
) -> None:
    pipe = Pipeline(tmp_path)
    pipe.setup = None
    reply = pipe.send({"skill": "ask", "question": "why?", "run_id": RUN_ID})
    assert reply["error"]["code"] == -32602 and "no coordinator is available" in str(reply["error"])
    assert pipe.rpc("ListTasks", {}).json()["result"].get("tasks", []) == []


def test_a_task_waiting_for_a_human_continues_on_the_same_task(pipe: Pipeline) -> None:
    first = pipe.send({"skill": "advance", "run_id": RUN_ID})["result"]["task"]
    assert _state(first) == "TASK_STATE_INPUT_REQUIRED"
    assert first["metadata"][RUN_ID_KEY] == RUN_ID
    again = pipe.send({"skill": "advance"}, taskId=first["id"], contextId=first["contextId"])
    assert again["result"]["task"]["id"] == first["id"]  # run_id from the task
    assert _state(again["result"]["task"]) == "TASK_STATE_INPUT_REQUIRED"


def test_a_query_naming_a_task_is_answered_by_message_and_leaves_the_task(pipe: Pipeline) -> None:
    first = pipe.send({"skill": "advance", "run_id": RUN_ID})["result"]["task"]
    # No contextId: the server takes the task's own.
    reply = pipe.send({"skill": "status"}, taskId=first["id"])["result"]["message"]
    assert reply["taskId"] == first["id"] and reply["contextId"] == first["contextId"]
    assert reply["parts"][0]["data"]["current"] == "H1"
    task = pipe.rpc("GetTask", {"id": first["id"]}).json()["result"]
    assert task["status"] == first["status"]


@pytest.mark.parametrize(
    "data",
    [
        {"skill": "approval_document"},  # missing arguments
        {"skill": "evidence"},  # refused by the pipeline (the seeded run has no evidence)
        {"skill": "decide"},
        {"skill": "start_run", "run_id": "run-9", "entry_spec": {}},
    ],
)
def test_a_refused_request_on_a_task_leaves_it_usable(pipe: Pipeline, data: dict) -> None:
    first = pipe.send({"skill": "advance", "run_id": RUN_ID})["result"]["task"]
    ids = {"taskId": first["id"], "contextId": first["contextId"]}
    assert pipe.send(data, **ids)["error"]["code"] == -32602
    task = pipe.rpc("GetTask", {"id": first["id"]}).json()["result"]
    assert _state(task) == "TASK_STATE_INPUT_REQUIRED"
    again = pipe.send({"skill": "advance"}, **ids)["result"]["task"]
    assert _state(again) == "TASK_STATE_INPUT_REQUIRED"


def test_a_refused_request_without_a_task_leaves_no_task_behind(pipe: Pipeline) -> None:
    for data in ({"skill": "status", "run_id": "nope"}, {"skill": "evidence", "run_id": RUN_ID}):
        assert pipe.send(data)["error"]["code"] == -32602
    assert pipe.send({"skill": "status", "run_id": RUN_ID})["result"]["message"]
    assert pipe.rpc("ListTasks", {}).json()["result"].get("tasks", []) == []


def test_a_run_that_cannot_be_opened_stops_the_task_resumably(pipe: Pipeline) -> None:
    def broken(root: Path, run_dir: Path) -> Any:
        raise RuntimeError("policy changed under the run")

    pipe.service.make_dispatcher = broken
    task = pipe.send({"skill": "advance", "run_id": RUN_ID})["result"]["task"]
    assert _state(task) == "TASK_STATE_INPUT_REQUIRED"
    assert "policy changed under the run" in _text(task) and "advance" in _text(task)


def test_start_run_creates_the_run_then_advances_it(tmp_path: Path) -> None:
    pipe = Pipeline(tmp_path)
    from test_grounding import SPEC as spec

    seed(tmp_path / "run-2", ("START", "G"))
    events = pipe.stream({"skill": "start_run", "run_id": "run-2", "entry_spec": spec})
    assert "task" in events[0] and events[0]["task"]["metadata"][RUN_ID_KEY] == "run-2"
    assert RunStore(tmp_path / "run-2").exists()
    assert pipe.send({"skill": "start_run", "run_id": "run-2", "entry_spec": spec})["error"]


def give_a_spec_no_model(root: Path) -> None:
    """A new `agents/*.md` that no entry owns (the learning spec's text, which has a brief)."""
    text = (ROOT / "agents" / "pipeline-learning-agent.md").read_text(encoding="utf-8")
    (root / "agents" / "pipeline-extra-helper-agent.md").write_text(text, encoding="utf-8")


def test_no_run_is_created_while_an_agent_has_no_model(tmp_path: Path) -> None:
    pipe = Pipeline(tmp_path)
    give_a_spec_no_model(registry_repo(pipe, tmp_path))
    from test_grounding import SPEC as spec

    reply = pipe.send({"skill": "start_run", "run_id": "run-2", "entry_spec": spec})
    assert reply["error"]["code"] == -32602
    message = reply["error"]["message"]
    assert (
        "MappingIncomplete: 1 agent has no model (agents/pipeline-extra-helper-agent.md)" in message
    )
    assert "complete_mapping" in message
    assert not (tmp_path / "run-2").exists(), "not even a directory"
    assert pipe.rpc("ListTasks", {}).json()["result"].get("tasks", []) == []


def test_the_streaming_start_the_editor_uses_creates_no_run_while_an_agent_has_no_model(
    tmp_path: Path,
) -> None:
    pipe = Pipeline(tmp_path)
    give_a_spec_no_model(registry_repo(pipe, tmp_path))
    from test_grounding import SPEC as spec

    pipe.stream({"skill": "start_run", "run_id": "run-2", "entry_spec": spec})
    assert not (tmp_path / "run-2").exists(), "not even a directory"
    assert pipe.rpc("ListTasks", {}).json()["result"].get("tasks", []) == []


def test_complete_mapping_with_every_agent_mapped_says_so_and_drafts_nothing(
    pipe: Pipeline, tmp_path: Path
) -> None:
    registry_repo(pipe, tmp_path)
    reply = data_of(pipe.send({"skill": "complete_mapping"}))
    assert reply == {"skill": "complete_mapping", "gaps": [], "change_id": None, "errors": []}
    assert not (tmp_path / ".changes").exists()


def test_the_gap_is_filled_by_a_draft_a_human_confirms_and_then_the_run_starts(
    tmp_path: Path,
) -> None:
    pipe = Pipeline(tmp_path)
    root = registry_repo(pipe, tmp_path)
    give_a_spec_no_model(root)
    draft = data_of(pipe.send({"skill": "complete_mapping"}))
    assert draft["errors"] == [] and draft["kind"] == "fill_gaps" and draft["proposed_by"] == "code"
    assert draft["gaps"] == [
        {
            "kind": "no_entry",
            "agent": "extra_helper",
            "spec": "agents/pipeline-extra-helper-agent.md",
        }
    ]
    assert "primary: gemini_3_8_flash_high" in draft["diff"]
    assert any("no stage wiring" in n for n in draft["notes"])
    again = data_of(pipe.send({"skill": "complete_mapping"}))
    assert again["change_id"] == draft["change_id"], "asking again does not pile up drafts"
    assert "extra_helper" not in (root / "policy" / "model_registry.yaml").read_text()
    applied = data_of(pipe.send({"skill": "apply_change", "change_id": draft["change_id"]}))
    assert applied["errors"] == []
    from test_grounding import SPEC as spec

    seed(tmp_path / "run-2", ("START", "G"))
    events = pipe.stream({"skill": "start_run", "run_id": "run-2", "entry_spec": spec})
    assert events[0]["task"]["metadata"][RUN_ID_KEY] == "run-2"
    assert RunStore(tmp_path / "run-2").exists()


def test_without_a_coordinator_the_gap_is_listed_with_the_way_to_name_one(
    tmp_path: Path,
) -> None:
    pipe = Pipeline(tmp_path)
    root = registry_repo(pipe, tmp_path)
    give_a_spec_no_model(root)
    (root / "LN-AGENTS.md").unlink()
    reply = data_of(pipe.send({"skill": "complete_mapping"}))
    assert reply["change_id"] is None and [g["agent"] for g in reply["gaps"]] == ["extra_helper"]
    assert "names no coordinator" in reply["errors"][0] and "`coordinator:`" in reply["errors"][0]


def test_complete_mapping_takes_no_arguments(pipe: Pipeline) -> None:
    assert pipe.send({"skill": "complete_mapping", "model": "x"})["error"]["code"] == -32602


def test_the_catalog_says_what_to_do_when_an_agent_has_no_model(
    pipe: Pipeline, tmp_path: Path
) -> None:
    give_a_spec_no_model(registry_repo(pipe, tmp_path))
    # the real catalog, which refuses a repository that fails validation
    pipe.service.catalog = lambda root: catalog_rows(root, Deps())
    reply = data_of(pipe.send({"skill": "models"}))
    assert reply["models"] == [] and reply["errors"][-1] == rc.NO_MODEL_HINT
    assert any("is not the spec of any agents entry" in e for e in reply["errors"])
    slots = data_of(pipe.send({"skill": "agents"}))
    assert slots["errors"] == [] and slots["slots"], "the mapping table still works"
    auto = data_of(pipe.send({"skill": "propose_mapping"}))
    assert auto["change_id"] is None and auto["errors"][-1] == rc.NO_MODEL_HINT, auto


def test_a_quota_pause_asks_for_auth(tmp_path: Path) -> None:
    pipe = Pipeline(tmp_path)
    pipe.adapters[A] = FakeAdapter(status="quota_exhausted")
    task = pipe.send({"skill": "advance", "run_id": RUN_ID})["result"]["task"]
    assert _state(task) == "TASK_STATE_AUTH_REQUIRED"
    assert "quota_exhausted" in _text(task) and "run resume" in _text(task)


def test_a_blocked_run_stays_resumable_not_failed(tmp_path: Path) -> None:
    pipe = Pipeline(tmp_path, handlers={"START": lambda ctx: "task_class_valid"})  # no G
    task = pipe.send({"skill": "advance", "run_id": RUN_ID})["result"]["task"]
    assert _state(task) == "TASK_STATE_INPUT_REQUIRED"
    assert "no handler registered for G" in _text(task) and "advance" in _text(task)
    # Not mistaken for a wait on a human, and not promised to continue on the next advance.
    assert _stop(task) == {"run_status": "blocked", "node": "G"}
    assert "blocks again until the cause is fixed" in _text(task)
    assert "Send advance to continue" not in _text(task)


def test_a_run_out_of_steps_says_advance_continues_it(pipe: Pipeline) -> None:
    task = pipe.send({"skill": "advance", "run_id": RUN_ID, "max_steps": 1})["result"]["task"]
    assert _state(task) == "TASK_STATE_INPUT_REQUIRED"
    assert _stop(task)["run_status"] == "out_of_steps"
    assert "Send advance to continue" in _text(task)


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
    assert found.status.message.parts[1].data.struct_value["run_status"] == "stopped"
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
