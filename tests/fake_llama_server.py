"""A stand-in for llama-server in tests: /health and /v1/models on loopback, nothing else.

    fake_llama_server.py --alias A --host 127.0.0.1 --port N [--delay S] [--exit-code C]

`--delay` waits before listening (a model still loading); `--exit-code` exits at once.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--alias", required=True)
    parser.add_argument("--host", required=True)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--delay", type=float, default=0.0)
    parser.add_argument("--exit-code", type=int)
    args = parser.parse_args()
    if args.exit_code is not None:
        print("fake llama-server: refusing to start", flush=True)
        return args.exit_code
    time.sleep(args.delay)
    models = json.dumps({"data": [{"id": args.alias}]}).encode()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            body = {"/health": b'{"status":"ok"}', "/v1/models": models}.get(self.path)
            self.send_response(200 if body else 404)
            self.end_headers()
            self.wfile.write(body or b"")

        def log_message(self, *_: object) -> None:
            pass

    print(f"fake llama-server listening on {args.host}:{args.port}", flush=True)
    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
