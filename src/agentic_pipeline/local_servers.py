"""Start the local model servers a stage needs, and stop them when it is done.

A local provider whose registry entry has `launch` (human-owned policy; R19 pins it to the
loopback host and port its readiness probe checks) no longer has to be started by hand. Before
a stage dispatches, `ensure` walks each operation's backends in preference order, as dispatch
will: the first one that is ready, or is not a local server, ends the walk, and a launchable
server that is down is started there and waited for. `release` stops what `ensure` started once
no stage in this process still uses it, so a 6 GB model is resident only while it is needed.

A server that was already running is used and never stopped. A server that does not come up
in time is killed and reported, and the stage is refused before anything is spent. argv runs
without a shell; only a leading `~` is expanded. Output goes to `<log_dir>/<provider>.log`.
"""

from __future__ import annotations

import subprocess
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any

from agentic_pipeline.readiness import Status, probe_llamacpp, probe_ovms
from agentic_pipeline.registry import backend_model, op_backends, operations

POLL_SECONDS = 0.5
STOP_GRACE_SECONDS = 5.0  # a Vulkan llama-server can ignore SIGTERM; then it is killed
DEFAULT_READY_SECONDS = 90.0
LOG_TAIL_BYTES = 2000
PROBES = {"llamacpp": probe_llamacpp, "ovms": probe_ovms}


class LocalServerError(Exception):
    """A server a stage needs could not be started; nothing was dispatched."""


@dataclass(slots=True)
class _Server:
    process: subprocess.Popen[bytes]
    log: IO[bytes]
    users: int = 0


# One table per process: the A2A server may run several runs, and a server one of them
# started must not be stopped while another stage still uses it.
_LOCK = threading.Lock()
_STARTING = threading.Lock()  # one start at a time, so two stages never start one server twice
_STARTED: dict[str, _Server] = {}


def ready(provider: Mapping[str, Any]) -> bool:
    """True when the provider's server answers its readiness probe."""
    spec = provider["readiness"]
    daemon, _ = PROBES[spec["protocol"]](spec, {})
    return daemon[0].status is Status.VERIFIED


@dataclass(slots=True)
class LocalServers:
    registry: Mapping[str, Any]
    log_dir: Path
    _using: list[str] = field(default_factory=list)

    def ensure(self, stage: str) -> None:
        """Start each launchable server this stage would dispatch to that is not running."""
        spec = self.registry["stage_requirements"].get(stage)
        if spec is None:
            return
        for _, op in operations({stage: spec}):
            for backend in op_backends(self.registry, stage, op):
                model = backend_model(self.registry, backend)
                name = model["provider"] if model else backend
                provider = self.registry["providers"].get(name) or {}
                if "protocol" not in (provider.get("readiness") or {}):
                    break  # a subscription tool: preflight decides, nothing to start
                with _STARTING:
                    if self._served(name, provider):
                        break
                    if "launch" in provider:
                        self._start(name, provider)
                        break

    def release(self) -> None:
        """Stop the servers this object started or shared once nothing else uses them."""
        with _LOCK:
            done = []
            for name in self._using:
                server = _STARTED.get(name)
                if server is None:
                    continue
                server.users -= 1
                if server.users <= 0:
                    done.append(_STARTED.pop(name))
            self._using.clear()
        for server in done:
            _stop(server)

    def _served(self, name: str, provider: Mapping[str, Any]) -> bool:
        """Running already: started here (now shared with this stage) or by someone else."""
        with _LOCK:
            if name in _STARTED and _STARTED[name].process.poll() is None:
                self._use(name)
                return True
        return ready(provider)

    def _use(self, name: str) -> None:
        _STARTED[name].users += 1
        self._using.append(name)

    def _start(self, name: str, provider: Mapping[str, Any]) -> None:
        launch = provider["launch"]
        argv = [str(Path(a).expanduser()) if a.startswith("~") else a for a in launch["argv"]]
        self.log_dir.mkdir(parents=True, exist_ok=True)
        log_path = self.log_dir / f"{name}.log"
        log = log_path.open("ab")
        try:
            process = subprocess.Popen(  # noqa: S603 - argv from human-owned policy, no shell
                argv,
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        except OSError as exc:
            log.close()
            raise LocalServerError(f"{name}: cannot start {argv[0]}: {exc}") from exc
        server = _Server(process, log)
        deadline = time.monotonic() + launch.get("ready_timeout_seconds", DEFAULT_READY_SECONDS)
        while not ready(provider):
            if process.poll() is not None:
                _stop(server)
                raise LocalServerError(
                    f"{name}: exited {process.returncode} while starting; {_tail(log_path)}"
                )
            if time.monotonic() > deadline:
                _stop(server)
                raise LocalServerError(f"{name}: not ready in time; {_tail(log_path)}")
            time.sleep(POLL_SECONDS)
        with _LOCK:
            _STARTED[name] = server
            self._use(name)


def _stop(server: _Server) -> None:
    process = server.process
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(STOP_GRACE_SECONDS)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
    server.log.close()


def _tail(path: Path) -> str:
    data = path.read_bytes()[-LOG_TAIL_BYTES:].decode("utf-8", "replace").strip()
    last = data.splitlines()[-3:] if data else []
    return f"log {path}: " + (" | ".join(last) or "(empty)")
