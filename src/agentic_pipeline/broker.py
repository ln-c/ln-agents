"""Host-side inference broker: the sandboxed worker's only route to a model.

The broker reads request frames from the in-sandbox shim over inherited pipes
(see sandbox_entry.py), validates each one against a grant-bound policy, and
only then forwards it to the configured loopback upstream. Frames, request
bodies, and model responses are all untrusted data. Every validated request
counts as one inference round; the policy caps rounds per invocation.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import http.client
import json
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass, field
from typing import Any

from agentic_pipeline.sandbox_entry import read_frame, write_frame

type Upstream = Callable[[str, bytes], tuple[int, Mapping[str, str], Iterator[bytes]]]

CHUNK_BYTES = 64 * 1024
FORWARDED_HEADERS = ("Content-Type",)


@dataclass(frozen=True, slots=True)
class BrokerPolicy:
    model_id: str
    max_rounds: int = 64
    max_request_bytes: int = 4 * 1024 * 1024
    max_response_bytes: int = 16 * 1024 * 1024
    allowed_paths: frozenset[str] = frozenset({"/v1/chat/completions"})
    # The model's registry defaults, merged into every forwarded request. They win over the
    # sandbox's own values, and the recorded request hash covers the bytes actually sent.
    chat_template_kwargs: Mapping[str, Any] | None = None


@dataclass(slots=True)
class BrokerStats:
    rounds: int = 0
    rejected: list[str] = field(default_factory=list)
    request_hashes: list[str] = field(default_factory=list)
    protocol_error: str | None = None


class Rejected(Exception):
    def __init__(self, status: int, reason: str) -> None:
        super().__init__(reason)
        self.status = status


def loopback_upstream(host: str, port: int, timeout: float) -> Upstream:
    """Forward to an OpenAI-compatible server on loopback (llama-server, or OVMS under /v3)."""

    def call(path: str, body: bytes) -> tuple[int, Mapping[str, str], Iterator[bytes]]:
        conn = http.client.HTTPConnection(host, port, timeout=timeout)
        conn.request("POST", path, body=body, headers={"Content-Type": "application/json"})
        resp = conn.getresponse()
        headers = {k: v for k in FORWARDED_HEADERS if (v := resp.getheader(k))}

        def chunks() -> Iterator[bytes]:
            try:
                while chunk := resp.read1(CHUNK_BYTES):
                    yield chunk
            finally:
                conn.close()

        return resp.status, headers, chunks()

    return call


def ovms_upstream(host: str, port: int, timeout: float) -> Upstream:
    """Forward to an OpenVINO Model Server on loopback.

    The sandbox's shim speaks `/v1/...` and the broker's allowed paths stay `/v1/...`; OVMS
    serves its OpenAI-compatible API under `/v3`, so the path is rewritten here, on the host.
    """
    forward = loopback_upstream(host, port, timeout)

    def call(path: str, body: bytes) -> tuple[int, Mapping[str, str], Iterator[bytes]]:
        return forward("/v3/" + path.removeprefix("/v1/"), body)

    return call


class InferenceBroker:
    def __init__(self, policy: BrokerPolicy, upstream: Upstream) -> None:
        self.policy = policy
        self.upstream = upstream
        self.stats = BrokerStats()

    def serve(self, req_fd: int, resp_fd: int) -> BrokerStats:
        """Answer frames until the shim closes its end; malformed frames end the session."""
        try:
            while (frame := read_frame(req_fd)) is not None:
                self._answer(frame, resp_fd)
        except (EOFError, ValueError, BrokenPipeError) as exc:
            self.stats.protocol_error = f"{type(exc).__name__}: {exc}"
        return self.stats

    def _answer(self, frame: Mapping[str, object], resp_fd: int) -> None:
        try:
            path, body = self._validate(frame)
        except Rejected as exc:
            self.stats.rejected.append(str(exc))
            self._respond(resp_fd, exc.status, iter([json.dumps({"error": str(exc)}).encode()]))
            return
        self.stats.rounds += 1
        self.stats.request_hashes.append("sha256:" + hashlib.sha256(body).hexdigest())
        try:
            status, headers, chunks = self.upstream(path, body)
        except OSError as exc:
            self._respond(resp_fd, 502, iter([json.dumps({"error": f"upstream: {exc}"}).encode()]))
            return
        self._respond(resp_fd, status, self._capped(chunks), headers)

    def _capped(self, chunks: Iterator[bytes]) -> Iterator[bytes]:
        """Relay at most max_response_bytes; a longer response is cut off and recorded."""
        sent = 0
        for chunk in chunks:
            sent += len(chunk)
            if sent > self.policy.max_response_bytes:
                self.stats.rejected.append(
                    f"response exceeded {self.policy.max_response_bytes} bytes; truncated"
                )
                return
            yield chunk

    def _validate(self, frame: Mapping[str, object]) -> tuple[str, bytes]:
        policy = self.policy
        if frame.get("method") != "POST":
            raise Rejected(405, f"method {frame.get('method')!r} not allowed")
        path = str(frame.get("path", "")).split("?", 1)[0]
        if path not in policy.allowed_paths:
            raise Rejected(403, f"path {path!r} not allowed")
        try:
            body = base64.b64decode(str(frame.get("body", "")), validate=True)
        except binascii.Error as exc:
            raise Rejected(400, "body is not valid base64") from exc
        if len(body) > policy.max_request_bytes:
            raise Rejected(413, f"request of {len(body)} bytes exceeds limit")
        try:
            request = json.loads(body)
        except ValueError as exc:
            raise Rejected(400, "body is not JSON") from exc
        if not isinstance(request, dict) or request.get("model") != policy.model_id:
            model = request.get("model") if isinstance(request, dict) else None
            raise Rejected(403, f"model {model!r} not permitted")
        if self.stats.rounds >= policy.max_rounds:
            raise Rejected(429, f"round limit {policy.max_rounds} reached")
        if policy.chat_template_kwargs:
            given = request.get("chat_template_kwargs")
            request["chat_template_kwargs"] = {
                **(given if isinstance(given, dict) else {}),
                **policy.chat_template_kwargs,
            }
            body = json.dumps(request, separators=(",", ":")).encode()
        return path, body

    @staticmethod
    def _respond(
        resp_fd: int,
        status: int,
        chunks: Iterator[bytes],
        headers: Mapping[str, str] | None = None,
    ) -> None:
        write_frame(
            resp_fd,
            {"status": status, "headers": dict(headers or {"Content-Type": "application/json"})},
        )
        for chunk in chunks:
            write_frame(resp_fd, {"chunk": base64.b64encode(chunk).decode("ascii")})
        write_frame(resp_fd, {"end": True})
