"""Tests for the pipe framing and the inference broker's validation (no sandbox)."""

from __future__ import annotations

import base64
import json
import os
import struct
import threading
from collections.abc import Iterator, Mapping
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

import pytest

from agentic_pipeline.broker import BrokerPolicy, InferenceBroker, ovms_upstream
from agentic_pipeline.sandbox_entry import MAX_FRAME_BYTES, read_frame, write_frame

MODEL = "qwen3:8b"


def test_frames_round_trip() -> None:
    r, w = os.pipe()
    write_frame(w, {"a": 1})
    write_frame(w, {"b": [2, 3]})
    os.close(w)
    assert [read_frame(r), read_frame(r), read_frame(r)] == [{"a": 1}, {"b": [2, 3]}, None]
    os.close(r)


@pytest.mark.parametrize(
    ("raw", "error"),
    [
        (struct.pack(">I", 10) + b"{}", EOFError),
        (struct.pack(">I", MAX_FRAME_BYTES + 1), ValueError),
        (struct.pack(">I", 2) + b"[]", ValueError),
        (b"\x00\x00", EOFError),
    ],
    ids=["truncated-body", "oversized", "not-an-object", "truncated-header"],
)
def test_malformed_frames_rejected(raw: bytes, error: type[Exception]) -> None:
    r, w = os.pipe()
    os.write(w, raw)
    os.close(w)
    with pytest.raises(error):
        read_frame(r)
    os.close(r)


class FakeUpstream:
    def __init__(self, chunks: tuple[bytes, ...] = (b'{"ok":', b"true}")) -> None:
        self.chunks = chunks
        self.calls: list[tuple[str, bytes]] = []

    def __call__(self, path: str, body: bytes) -> tuple[int, Mapping[str, str], Iterator[bytes]]:
        self.calls.append((path, body))
        return 200, {"Content-Type": "application/json"}, iter(self.chunks)


def exchange(
    broker: InferenceBroker, requests: list[dict[str, Any] | bytes]
) -> list[tuple[int, bytes]]:
    """Send request frames (or raw bytes) to a serving broker; collect (status, body) replies."""
    req_r, req_w = os.pipe()
    resp_r, resp_w = os.pipe()
    serving = threading.Thread(target=broker.serve, args=(req_r, resp_w))
    serving.start()
    replies = []
    for request in requests:
        if isinstance(request, bytes):
            os.write(req_w, request)
            break
        write_frame(req_w, request)
        head = read_frame(resp_r)
        body = b""
        while not (frame := read_frame(resp_r)).get("end"):
            body += base64.b64decode(frame["chunk"])
        replies.append((head["status"], body))
    os.close(req_w)
    serving.join(5)
    for fd in (req_r, resp_r, resp_w):
        os.close(fd)
    return replies


def request(
    body: Any = None, path: str = "/v1/chat/completions", method: str = "POST"
) -> dict[str, Any]:
    payload = body if isinstance(body, bytes) else json.dumps(body or {"model": MODEL}).encode()
    return {"method": method, "path": path, "body": base64.b64encode(payload).decode()}


def test_valid_request_forwarded_and_streamed() -> None:
    upstream = FakeUpstream()
    broker = InferenceBroker(BrokerPolicy(MODEL), upstream)
    assert exchange(broker, [request()]) == [(200, b'{"ok":true}')]
    assert upstream.calls[0][0] == "/v1/chat/completions"
    assert broker.stats.rounds == 1 and len(broker.stats.request_hashes) == 1


@pytest.mark.parametrize(
    ("frame", "status"),
    [
        (request(method="GET"), 405),
        (request(path="/api/pull"), 403),
        (request(path="/v1/chat/completions/../../api/delete"), 403),
        (request({"model": "llama3"}), 403),
        (request({"messages": []}), 403),
        (request(b"not json"), 400),
        ({"method": "POST", "path": "/v1/chat/completions", "body": "%%%"}, 400),
    ],
    ids=["method", "path", "path-traversal", "foreign-model", "no-model", "not-json", "bad-b64"],
)
def test_invalid_requests_rejected_without_upstream_call(frame: dict, status: int) -> None:
    upstream = FakeUpstream()
    broker = InferenceBroker(BrokerPolicy(MODEL), upstream)
    assert exchange(broker, [frame])[0][0] == status
    assert upstream.calls == [] and broker.stats.rounds == 0 and broker.stats.rejected


def test_oversized_request_rejected() -> None:
    broker = InferenceBroker(BrokerPolicy(MODEL, max_request_bytes=64), FakeUpstream())
    padded = {"model": MODEL, "messages": ["x" * 100]}
    assert exchange(broker, [request(padded)])[0][0] == 413


def test_round_limit_enforced() -> None:
    upstream = FakeUpstream()
    broker = InferenceBroker(BrokerPolicy(MODEL, max_rounds=2), upstream)
    statuses = [s for s, _ in exchange(broker, [request()] * 3)]
    assert statuses == [200, 200, 429] and len(upstream.calls) == 2


def test_upstream_failure_is_502() -> None:
    def down(path: str, body: bytes) -> Any:
        raise ConnectionRefusedError("ollama stopped")

    assert exchange(InferenceBroker(BrokerPolicy(MODEL), down), [request()])[0][0] == 502


def test_malformed_frame_ends_session() -> None:
    broker = InferenceBroker(BrokerPolicy(MODEL), FakeUpstream())
    exchange(broker, [struct.pack(">I", 5) + b"[1]  "])
    assert broker.stats.protocol_error is not None


def test_oversized_response_truncated_and_recorded() -> None:
    broker = InferenceBroker(
        BrokerPolicy(MODEL, max_response_bytes=10), FakeUpstream((b"0123456", b"789abc", b"def"))
    )
    status, body = exchange(broker, [request()])[0]
    assert status == 200 and body == b"0123456"
    assert any("response exceeded" in r for r in broker.stats.rejected)


def test_ovms_upstream_rewrites_the_path_to_v3_on_the_host() -> None:
    seen: list[tuple[str, bytes]] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802 - http.server API
            body = self.rfile.read(int(self.headers["Content-Length"]))
            seen.append((self.path, body))
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b'{"ok": true}')

        def log_message(self, *args: object) -> None:
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        upstream = ovms_upstream("127.0.0.1", server.server_address[1], timeout=5.0)
        status, headers, chunks = upstream("/v1/chat/completions", b'{"model": "m"}')
        assert status == 200 and b"".join(chunks) == b'{"ok": true}'
        assert headers == {"Content-Type": "application/json"}
        assert seen == [("/v3/chat/completions", b'{"model": "m"}')]
    finally:
        server.shutdown()
        server.server_close()


def test_the_broker_still_allows_only_the_v1_chat_path_for_ovms() -> None:
    """The rewrite happens after validation: a worker cannot name a /v3 path itself."""
    assert BrokerPolicy("m").allowed_paths == frozenset({"/v1/chat/completions"})
