"""Local model servers started on demand from the registry's `launch`, and stopped after."""

from __future__ import annotations

import copy
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from test_dispatch import REGISTRY, FakeAdapter, adapters, events, make  # noqa: F401

from agentic_pipeline import local_servers
from agentic_pipeline.dispatch import DispatchBlocked
from agentic_pipeline.local_servers import LocalServerError, LocalServers, ready

FAKE = Path(__file__).resolve().parent / "fake_llama_server.py"
PROVIDER = "llamacpp_decision"  # Clef-Flash, which A2J dispatches to


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def registry_with(*extra: str, timeout: float = 20.0) -> dict[str, Any]:
    """The repo registry with the decision server played by the fake, on a free port."""
    registry = copy.deepcopy(REGISTRY)
    provider = registry["providers"][PROVIDER]
    port = free_port()
    provider["readiness"]["port"] = port
    argv = [sys.executable, str(FAKE), "--alias", "clef-flash", "--host", "127.0.0.1"]
    provider["launch"] = {
        "argv": [*argv, "--port", str(port), *extra],
        "ready_timeout_seconds": timeout,
    }
    return registry


@pytest.fixture(autouse=True)
def nothing_left_running() -> Iterator[None]:
    yield
    leftover = dict(local_servers._STARTED)
    local_servers._STARTED.clear()
    for server in leftover.values():
        local_servers._stop(server)
    assert not leftover, f"servers left running: {sorted(leftover)}"


def test_a_stage_starts_its_server_and_stops_it_after(tmp_path: Path) -> None:
    registry = registry_with()
    provider = registry["providers"][PROVIDER]
    servers = LocalServers(registry, tmp_path / "logs")
    assert not ready(provider)
    servers.ensure("A2J")
    assert ready(provider)
    servers.release()
    assert not ready(provider)
    assert "listening" in (tmp_path / "logs" / f"{PROVIDER}.log").read_text()


def test_a_server_already_running_is_used_and_left_running(tmp_path: Path) -> None:
    registry = registry_with()
    provider = registry["providers"][PROVIDER]
    mine = subprocess.Popen(provider["launch"]["argv"])  # noqa: S603 - the test's own fake
    try:
        deadline = time.monotonic() + 20
        while not ready(provider) and time.monotonic() < deadline:
            time.sleep(0.1)
        servers = LocalServers(registry, tmp_path / "logs")
        servers.ensure("A2J")
        servers.release()
        assert ready(provider) and mine.poll() is None
        assert not (tmp_path / "logs").exists()  # it started nothing
    finally:
        mine.kill()
        mine.wait()


def test_two_stages_share_one_server_until_both_are_done(tmp_path: Path) -> None:
    registry = registry_with()
    provider = registry["providers"][PROVIDER]
    first, second = (LocalServers(registry, tmp_path / "logs") for _ in range(2))
    first.ensure("A2J")
    second.ensure("A2J")
    first.release()
    assert ready(provider)
    second.release()
    assert not ready(provider)


def test_a_server_that_exits_is_reported_with_its_log(tmp_path: Path) -> None:
    servers = LocalServers(registry_with("--exit-code", "3"), tmp_path / "logs")
    with pytest.raises(LocalServerError, match="exited 3 while starting.*refusing to start"):
        servers.ensure("A2J")


def test_a_server_too_slow_to_start_is_stopped(tmp_path: Path) -> None:
    registry = registry_with("--delay", "30", timeout=1.0)
    servers = LocalServers(registry, tmp_path / "logs")
    with pytest.raises(LocalServerError, match="not ready in time"):
        servers.ensure("A2J")
    assert not local_servers._STARTED


def test_a_stage_on_a_cloud_tool_starts_nothing(tmp_path: Path) -> None:
    servers = LocalServers(registry_with(), tmp_path / "logs")
    servers.ensure("A1")
    servers.release()
    assert not (tmp_path / "logs").exists()


def test_dispatch_runs_with_the_server_up_and_stops_it_after(
    tmp_path: Path,
    adapters: dict[str, FakeAdapter],  # noqa: F811
) -> None:
    registry = registry_with()
    provider = registry["providers"][PROVIDER]
    seen: list[bool] = []
    d = make(tmp_path, adapters)
    d.servers = LocalServers(registry, tmp_path / "logs")
    judge = REGISTRY["agents"]["design"]["finding_triage"]["primary"]
    adapters[judge].side_effect = lambda: seen.append(ready(provider))
    outcome = d.dispatch_stage("A2", "always", "A2J", {})
    assert not outcome.provisional and seen == [True]
    assert not ready(provider)


def test_a_server_that_cannot_start_refuses_the_stage_before_anything_is_spent(
    tmp_path: Path,
    adapters: dict[str, FakeAdapter],  # noqa: F811
) -> None:
    d = make(tmp_path, adapters)
    d.servers = LocalServers(registry_with("--exit-code", "1"), tmp_path / "logs")
    with pytest.raises(DispatchBlocked, match=f"A2J: {PROVIDER}: exited 1"):
        d.dispatch_stage("A2", "always", "A2J", {})
    assert events(d) == [] and d.ledger.reserved == {} and d.ledger.consumed == {}
