"""The decision-model adapter (/v1/systemone) and the `decision_probe` conformance check.

The model is a stand-in upstream: no test here needs a running llama-server.
"""
# ruff: noqa: E501

from __future__ import annotations

import hashlib
import json
import threading
from collections.abc import Iterator, Mapping
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any

import pytest

from agentic_pipeline.adapters import (
    DecisionAdapter,
    answer_problems,
    request_problems,
    requests_from_inputs,
)
from agentic_pipeline.conformance import ProbeEnv, check_decision_probe
from agentic_pipeline.dispatch import DispatchGrant
from agentic_pipeline.graph import load_yaml

ROOT = Path(__file__).resolve().parents[1]
MODEL = "clef-flash"
NONCE = "cd" * 16
NOUL = {"type": "noul", "instructions": "Is the door red?"}
CHOICE = {
    "type": "choice",
    "instructions": "What colour is the door?",
    "criteria": {"red": None, "blue": "like the sky"},
}
SCORE = {"type": "score", "instructions": "How sure?", "criteria": ["low", "mid", "high"]}
QUESTIONS = {"q1": NOUL, "q2": CHOICE, "q3": SCORE}
REQUEST = {"id": "r1", "state": "The door is red.", "questions": QUESTIONS}


def answers_for(questions: Mapping[str, Any], yes: float = 0.9) -> dict[str, Any]:
    """What a compliant decision model returns for `questions`."""
    out: dict[str, Any] = {}
    for name, q in questions.items():
        if q["type"] == "noul":
            out[name] = {"type": "noul", "noul": yes}
        elif q["type"] == "choice":
            options = list(q["criteria"])
            probs = {
                o: (0.8 if i == 0 else 0.2 / (len(options) - 1)) for i, o in enumerate(options)
            }
            out[name] = {
                "type": "choice",
                "choice": options[0],
                "probabilities": probs,
                "confidence": 0.7,
            }
        else:
            levels = q["criteria"]
            probs = {str(i): 1 / len(levels) for i in range(len(levels))}
            out[name] = {
                "type": "score",
                "score": 1.0,
                "legend": {str(i): level for i, level in enumerate(levels)},
                "probabilities": probs,
                "confidence": 0.5,
            }
    return out


class Upstream:
    """Stands in for llama-server: records each POST and answers like a decision model."""

    def __init__(self, status: int = 200, reply: Any = None, raises: bool = False) -> None:
        self.status, self.reply, self.raises = status, reply, raises
        self.sent: list[tuple[str, dict[str, Any]]] = []

    def __call__(self, path: str, body: bytes) -> tuple[int, Mapping[str, str], Iterator[bytes]]:
        request = json.loads(body)
        self.sent.append((path, request))
        if self.raises:
            raise ConnectionRefusedError("connection refused")
        reply = self.reply
        if reply is None:
            reply = {
                "model": MODEL,
                "answers": answers_for(request["questions"]),
                "usage": {"input_tokens": 40, "output_tokens": 0},
            }
        raw = reply if isinstance(reply, bytes) else json.dumps(reply).encode()
        return self.status, {}, iter([raw[:5], raw[5:]])


def grant(tmp_path: Path, requests: list[Any] | None = None) -> DispatchGrant:
    source = tmp_path / "in" / "requests.json"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_text(json.dumps({"requests": [REQUEST] if requests is None else requests}))
    write_path = tmp_path / "out"
    write_path.mkdir(exist_ok=True)
    return DispatchGrant(
        run_id="run-1",
        stage="CONFORMANCE",
        operation_id="verdicts",
        backend="clef_flash_local",
        invocation_id="inv-1",
        role="judge",
        allowed_reads=("probe_input",),
        inputs={"probe_input": source},
        data_class="internal",
        write_root="conformance",
        attempt="A-1",
        write_path=write_path,
    )


def adapter(upstream: Upstream | None = None) -> DecisionAdapter:
    return DecisionAdapter(MODEL, f"{MODEL}@sha256:abc (llama.cpp b1)", upstream or Upstream())


# ── Requests ─────────────────────────────────────────────────────────────────


def test_a_well_formed_request_has_no_problems() -> None:
    assert request_problems(REQUEST) == []


@pytest.mark.parametrize(
    ("request_", "problem"),
    [
        ({"id": "r", "state": "s"}, "exactly the keys"),
        ({**REQUEST, "extra": 1}, "exactly the keys"),
        ({**REQUEST, "id": ""}, "id must be"),
        ({**REQUEST, "state": "  "}, "state must be"),
        ({**REQUEST, "questions": {}}, "non-empty object"),
        ({**REQUEST, "questions": {"q": {"type": "essay", "instructions": "x"}}}, "type must be"),
        ({**REQUEST, "questions": {"q": {**NOUL, "criteria": {}}}}, "unexpected or missing keys"),
        (
            {**REQUEST, "questions": {"q": {"type": "choice", "instructions": "x"}}},
            "unexpected or missing",
        ),
        ({**REQUEST, "questions": {"q": {**NOUL, "instructions": ""}}}, "instructions must be"),
        (
            {**REQUEST, "questions": {"q": {**CHOICE, "criteria": {"only": None}}}},
            "2 or more options",
        ),
        (
            {**REQUEST, "questions": {"q": {**CHOICE, "criteria": {"a": 1, "b": None}}}},
            "2 or more options",
        ),
        ({**REQUEST, "questions": {"q": {**SCORE, "criteria": ["one"]}}}, "2 to 10 distinct"),
        ({**REQUEST, "questions": {"q": {**SCORE, "criteria": ["a", "a"]}}}, "2 to 10 distinct"),
        (
            {**REQUEST, "questions": {"q": {**SCORE, "criteria": [str(i) for i in range(11)]}}},
            "2 to 10",
        ),
    ],
)
def test_malformed_requests_are_refused(request_: Any, problem: str) -> None:
    assert any(problem in p for p in request_problems(request_)), request_problems(request_)


def test_prepare_hashes_the_request_list_as_sent(tmp_path: Path) -> None:
    call = adapter().prepare(grant(tmp_path))
    assert json.loads(call.prompt) == [REQUEST]
    assert list(json.loads(call.prompt)[0]["questions"]["q2"]["criteria"]) == ["red", "blue"]
    assert call.model == MODEL and "systemone" in call.access_method
    again = adapter().prepare(grant(tmp_path / "b"))
    assert again.prompt_hash == call.prompt_hash


@pytest.mark.parametrize(
    ("requests", "error"),
    [
        ([], "no decision requests"),
        ([{**REQUEST, "state": ""}], "state must be"),
        ([REQUEST, REQUEST], "not unique"),
    ],
)
def test_prepare_refuses_what_it_would_not_send(
    tmp_path: Path, requests: list[Any], error: str
) -> None:
    with pytest.raises(ValueError, match=error):
        adapter().prepare(grant(tmp_path, requests))


def test_requests_from_inputs_needs_a_requests_list(tmp_path: Path) -> None:
    g = grant(tmp_path)
    g.inputs["probe_input"].write_text('{"questions": {}}')
    with pytest.raises(ValueError, match="requests list"):
        requests_from_inputs(g)


# ── Answers ──────────────────────────────────────────────────────────────────


def test_compliant_answers_have_no_problems() -> None:
    assert answer_problems(QUESTIONS, answers_for(QUESTIONS)) == []


def _broken(name: str, **changes: Any) -> dict[str, Any]:
    answers = answers_for(QUESTIONS)
    answers[name] = {**answers[name], **changes}
    return answers


@pytest.mark.parametrize(
    ("answers", "problem"),
    [
        ({"q1": answers_for(QUESTIONS)["q1"]}, "exactly the questions"),
        ({**answers_for(QUESTIONS), "q9": {}}, "exactly the questions"),
        (_broken("q1", type="choice"), "not a noul answer"),
        (_broken("q1", noul=1.5), "not a probability"),
        (_broken("q1", noul=True), "not a probability"),
        (_broken("q1", note="trust me"), "unexpected or missing keys"),
        (_broken("q2", choice="green"), "was not offered"),
        (_broken("q2", probabilities={"red": 0.9}), "cover exactly the options"),
        (_broken("q2", probabilities={"red": 0.9, "blue": -0.1}), "outside [0, 1]"),
        (_broken("q2", confidence=2), "confidence"),
        (_broken("q3", score=3.5), "outside the levels"),
        (_broken("q3", legend={"0": "low", "1": "mid", "2": "max"}), "legend"),
        (_broken("q3", probabilities={"1": 0.5, "2": 0.5, "3": 0.0}), "cover exactly the options"),
    ],
)
def test_answers_that_do_not_match_the_questions_are_refused(answers: Any, problem: str) -> None:
    found = answer_problems(QUESTIONS, answers)
    assert any(problem in p for p in found), found


# ── Invoke ───────────────────────────────────────────────────────────────────


def test_invoke_sends_the_prepared_requests_and_writes_the_probabilities(tmp_path: Path) -> None:
    upstream = Upstream()
    second = {**REQUEST, "id": "r2", "questions": {"q1": NOUL}}
    g = grant(tmp_path, [REQUEST, second])
    built = adapter(upstream)
    result = built.invoke(g, built.prepare(g))
    assert (result.status, result.detail) == ("ok", "requests=2 input_tokens=80")
    assert result.resolved_model == MODEL
    assert [path for path, _ in upstream.sent] == ["/v1/systemone"] * 2
    assert upstream.sent[0][1] == {
        "model": MODEL,
        "state": REQUEST["state"],
        "questions": QUESTIONS,
    }
    document = json.loads((g.write_path / "verdicts.json").read_text())
    assert document["model"] == MODEL and document["model_version"].startswith(MODEL)
    assert [r["id"] for r in document["results"]] == ["r1", "r2"]
    assert document["results"][0]["answers"]["q1"] == {"type": "noul", "noul": 0.9}
    assert document["results"][0]["input_tokens"] == 40


@pytest.mark.parametrize(
    ("upstream", "detail"),
    [
        (Upstream(raises=True), "ConnectionRefusedError"),
        (
            Upstream(400, {"error": {"code": 400, "message": "input too large"}}),
            "HTTP 400: input too large",
        ),
        (Upstream(500, b"oops"), "HTTP 500: not JSON"),
        (Upstream(reply={"model": "other", "answers": {}}), "answered by 'other'"),
        (
            Upstream(reply={"model": MODEL, "answers": {"q1": {"type": "noul", "noul": 0.5}}}),
            "exactly the questions",
        ),
        (Upstream(reply=b"x" * 2_000_000), "response over"),
    ],
)
def test_invoke_fails_closed_and_writes_nothing(
    tmp_path: Path, upstream: Upstream, detail: str
) -> None:
    g = grant(tmp_path)
    built = adapter(upstream)
    result = built.invoke(g, built.prepare(g))
    assert result.status == "error" and detail in result.detail, result.detail
    assert list(g.write_path.iterdir()) == []


# ── From the registry, against a stand-in llama-server ───────────────────────


@pytest.fixture
def server(tmp_path: Path) -> Iterator[dict[str, Any]]:
    gguf = tmp_path / "clef.gguf"
    gguf.write_bytes(b"GGUF decision weights")
    state: dict[str, Any] = {"modalities": ["decisions"], "gguf": gguf}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802 - http.server API
            entry = {"id": MODEL, "architecture": {"output_modalities": state["modalities"]}}
            body = {
                "/v1/models": {"data": [entry]},
                "/props": {"model_path": str(gguf), "build_info": "b11429-d8"},
            }.get(self.path)
            self.send_response(200 if body else 404)
            self.end_headers()
            self.wfile.write(json.dumps(body or {}).encode())

        def do_POST(self) -> None:  # noqa: N802 - http.server API
            request = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            reply = {"model": MODEL, "answers": answers_for(request["questions"]), "usage": {}}
            self.send_response(200)
            self.end_headers()
            self.wfile.write(json.dumps(reply).encode())

        def log_message(self, *args: object) -> None:
            pass

    httpd = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    registry = load_yaml(ROOT / "policy" / "model_registry.yaml")
    registry["providers"]["llamacpp_decision"]["readiness"]["port"] = httpd.server_address[1]
    state["registry"] = registry
    yield state
    httpd.shutdown()
    httpd.server_close()


def test_from_registry_pins_the_model_file_and_build_and_talks_to_the_server(
    server: dict[str, Any], tmp_path: Path
) -> None:
    built = DecisionAdapter.from_registry(server["registry"], "clef_flash_local")
    digest = hashlib.sha256(b"GGUF decision weights").hexdigest()
    assert built.identity().model_version == f"{MODEL}@sha256:{digest} (llama.cpp b11429-d8)"
    g = grant(tmp_path)
    assert built.invoke(g, built.prepare(g)).status == "ok"


def test_from_registry_refuses_a_server_that_does_not_serve_decisions(
    server: dict[str, Any],
) -> None:
    server["modalities"] = ["text"]
    with pytest.raises(LookupError, match="not a decision model"):
        DecisionAdapter.from_registry(server["registry"], "clef_flash_local")
    server["registry"]["models"]["clef_flash_local"]["model_id"] = "other"
    with pytest.raises(LookupError, match="not served"):
        DecisionAdapter.from_registry(server["registry"], "clef_flash_local")


# ── The decision_probe conformance check ─────────────────────────────────────


def probe(tmp_path: Path, upstream: Upstream) -> tuple[bool, str]:
    return check_decision_probe(ProbeEnv("clef_flash_local", adapter(upstream), tmp_path, NONCE))


class Judge(Upstream):
    """Answers the probe's questions from what its state says, or wrongly if told to."""

    def __init__(self, wrong: bool = False) -> None:
        super().__init__()
        self.wrong = wrong

    def __call__(self, path: str, body: bytes) -> tuple[int, Mapping[str, str], Iterator[bytes]]:
        request = json.loads(body)
        assert NONCE in request["state"]
        answers = answers_for(request["questions"])
        answers["yes"]["noul"], answers["no"]["noul"] = (0.2, 0.9) if self.wrong else (0.95, 0.03)
        self.reply = {"model": MODEL, "answers": answers, "usage": {"input_tokens": 60}}
        return super().__call__(path, body)


def test_the_probe_passes_a_model_that_reads_the_record(tmp_path: Path) -> None:
    assert probe(tmp_path, Judge()) == (True, "yes=0.950 no=0.030 colour=red")


def test_the_probe_fails_wrong_answers(tmp_path: Path) -> None:
    ok, detail = probe(tmp_path, Judge(wrong=True))
    assert not ok and detail.startswith("wrong answers: yes=0.200 no=0.900")


def test_the_probe_reports_an_unreachable_server(tmp_path: Path) -> None:
    ok, detail = probe(tmp_path, Upstream(raises=True))
    assert not ok and "ConnectionRefusedError" in detail
