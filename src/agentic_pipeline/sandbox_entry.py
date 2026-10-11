"""In-sandbox inference shim and worker launcher. Runs INSIDE bwrap as `python3 -I`.

Standalone by design: imports only the standard library, never agentic_pipeline,
because the sandbox sees this one file and nothing else from the package.

    sandbox_entry.py -- <worker argv...>     launch worker behind the shim
    sandbox_entry.py --probe <home> <model>  preflight self-test worker

The shim serves HTTP on the sandbox's private loopback and relays each request
over inherited pipes to the host-side InferenceBroker, which alone can reach the
model. The worker gets the shim URL in AP_INFERENCE_URL and never sees the pipe
fds. Frames are a 4-byte big-endian length followed by UTF-8 JSON.
"""

from __future__ import annotations

import base64
import json
import os
import struct
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

MAX_FRAME_BYTES = 16 * 1024 * 1024
MAX_BODY_BYTES = 8 * 1024 * 1024
REQ_FD_ENV = "AP_BROKER_REQ_FD"
RESP_FD_ENV = "AP_BROKER_RESP_FD"
URL_ENV = "AP_INFERENCE_URL"
# Fixed: the sandbox has a private network namespace, so nothing else can hold it.
SHIM_PORT = 8411
SHIM_URL = f"http://127.0.0.1:{SHIM_PORT}/v1"


def write_frame(fd: int, obj: object) -> None:
    data = json.dumps(obj, separators=(",", ":")).encode("utf-8")
    view = memoryview(struct.pack(">I", len(data)) + data)
    while view:
        view = view[os.write(fd, view) :]


def _read_exact(fd: int, n: int) -> bytes | None:
    buf = b""
    while len(buf) < n:
        chunk = os.read(fd, n - len(buf))
        if not chunk:
            if buf:
                raise EOFError("truncated frame")
            return None
        buf += chunk
    return buf


def read_frame(fd: int) -> dict | None:
    """Next frame, or None at a clean end of stream."""
    header = _read_exact(fd, 4)
    if header is None:
        return None
    (size,) = struct.unpack(">I", header)
    if size > MAX_FRAME_BYTES:
        raise ValueError(f"frame of {size} bytes exceeds limit")
    body = _read_exact(fd, size)
    if body is None:
        raise EOFError("truncated frame")
    frame = json.loads(body)
    if not isinstance(frame, dict):
        raise ValueError("frame is not a JSON object")
    return frame


def _relay_handler(req_fd: int, resp_fd: int) -> type[BaseHTTPRequestHandler]:
    lock = threading.Lock()

    class Relay(BaseHTTPRequestHandler):
        def _relay(self) -> None:
            length = int(self.headers.get("Content-Length") or 0)
            if length > MAX_BODY_BYTES:
                self.send_error(413)
                return
            body = self.rfile.read(length) if length else b""
            with lock:
                write_frame(
                    req_fd,
                    {
                        "method": self.command,
                        "path": self.path,
                        "body": base64.b64encode(body).decode("ascii"),
                    },
                )
                head = read_frame(resp_fd)
                if head is None:
                    self.send_error(502)
                    return
                self.send_response(int(head["status"]))
                for key, value in (head.get("headers") or {}).items():
                    self.send_header(key, value)
                self.end_headers()
                while (frame := read_frame(resp_fd)) is not None and not frame.get("end"):
                    self.wfile.write(base64.b64decode(frame["chunk"]))
                    self.wfile.flush()

        do_GET = do_POST = do_PUT = do_DELETE = _relay

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            pass

    return Relay


def launch(argv: list[str]) -> int:
    req_fd, resp_fd = int(os.environ[REQ_FD_ENV]), int(os.environ[RESP_FD_ENV])
    server = HTTPServer(("127.0.0.1", SHIM_PORT), _relay_handler(req_fd, resp_fd))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    env = {k: v for k, v in os.environ.items() if k not in (REQ_FD_ENV, RESP_FD_ENV)}
    env[URL_ENV] = SHIM_URL
    try:
        # close_fds (the default) keeps the broker pipes away from the worker.
        return subprocess.run(argv, env=env, check=False).returncode  # noqa: S603
    finally:
        server.shutdown()
        os.close(req_fd)


def _post(url: str, model: str) -> list:
    body = json.dumps({"model": model, "messages": []}).encode("utf-8")
    request = urllib.request.Request(  # noqa: S310 - fixed http://127.0.0.1 shim URL
        url, data=body, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(request, timeout=10) as resp:  # noqa: S310
            return [resp.status, resp.read().decode("utf-8", "replace")]
    except urllib.error.HTTPError as exc:
        return [exc.code, ""]
    except OSError as exc:
        return [0, str(exc)]


def probe(host_home: str, model: str) -> int:
    """Report what the sandbox permits, as one JSON line, for preflight."""
    with open("/proc/net/dev", encoding="ascii") as fh:
        interfaces = sorted(line.split(":")[0].strip() for line in fh.read().splitlines()[2:])
    url = os.environ[URL_ENV] + "/chat/completions"
    report = {
        "write_root_writable": os.access("/work", os.W_OK),
        "input_writable": os.access("/inputs/probe", os.W_OK),
        "host_home_visible": os.path.exists(host_home),
        "interfaces": interfaces,
        "brokered": _post(url, model),
        "foreign_model": _post(url, model + "-not-allowed"),
    }
    print(json.dumps(report))
    return 0


def main(args: list[str]) -> int:
    if args[:1] == ["--probe"] and len(args) == 3:
        return probe(args[1], args[2])
    if args[:1] == ["--"] and len(args) > 1:
        return launch(args[1:])
    print("usage: sandbox_entry.py -- <argv...> | --probe <home> <model>", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
