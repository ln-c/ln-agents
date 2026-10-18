"""File locks and reads that must behave the same on Linux, macOS and Windows."""

from __future__ import annotations

import json
import os
import threading
from collections.abc import Callable
from pathlib import Path

import pytest

from agentic_pipeline import fsutil
from agentic_pipeline.fsutil import (
    O_BINARY,
    atomic_write_json,
    lock,
    read_all,
    retry_sharing,
    unlock,
)


def _open(path: Path) -> int:
    return os.open(path, os.O_RDWR | os.O_CREAT | O_BINARY, 0o600)


def test_a_held_lock_refuses_a_second_holder_until_released(tmp_path: Path) -> None:
    path = tmp_path / "lock"
    first, second = _open(path), _open(path)
    try:
        lock(first, blocking=False)
        with pytest.raises(BlockingIOError):
            lock(second, blocking=False)
        unlock(first)
        lock(second, blocking=False)  # free again
        unlock(second)
    finally:
        os.close(first)
        os.close(second)


def test_a_locked_file_stays_readable_and_appendable(tmp_path: Path) -> None:
    """Windows locks are mandatory, so the lock must not cover the bytes readers read."""
    path = tmp_path / "log.jsonl"
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_APPEND | O_BINARY, 0o600)
    try:
        lock(fd, blocking=True)
        os.write(fd, b'{"seq": 0}\n')
        assert path.read_bytes() == b'{"seq": 0}\n'  # another reader, while the lock is held
        os.write(fd, b'{"seq": 1}\n')
        assert read_all(fd) == b'{"seq": 0}\n{"seq": 1}\n'
        unlock(fd)
    finally:
        os.close(fd)


def test_writes_are_binary_on_every_platform(tmp_path: Path) -> None:
    path = tmp_path / "bytes"
    fd = _open(path)
    try:
        os.write(fd, b"a\nb\n")
    finally:
        os.close(fd)
    assert path.read_bytes() == b"a\nb\n"  # no "\r\n" translation on Windows


def _flaky(failures: int) -> tuple[list[int], Callable[[], str]]:
    calls: list[int] = []

    def operation() -> str:
        calls.append(1)
        if len(calls) <= failures:
            raise PermissionError("in use by another process")
        return "done"

    return calls, operation


def test_a_sharing_violation_on_windows_is_retried(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fsutil, "WINDOWS", True)
    calls, operation = _flaky(2)
    assert retry_sharing(operation) == "done"
    assert len(calls) == 3


def test_a_lasting_permission_error_is_raised(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fsutil, "WINDOWS", True)
    monkeypatch.setattr(fsutil, "_SHARING_RETRY_SECONDS", 0.05)
    _, operation = _flaky(10**6)
    with pytest.raises(PermissionError):
        retry_sharing(operation)


def test_posix_never_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(fsutil, "WINDOWS", False)
    calls, operation = _flaky(1)
    with pytest.raises(PermissionError):
        retry_sharing(operation)
    assert len(calls) == 1


def test_readers_and_a_replacing_writer_do_not_fail_each_other(tmp_path: Path) -> None:
    """The A2A server reads run state while the runner replaces it (a real race on Windows)."""
    path = tmp_path / "state.json"
    atomic_write_json(path, {"n": 0})
    errors: list[BaseException] = []

    def write() -> None:
        try:
            for n in range(1, 201):
                atomic_write_json(path, {"n": n})
        except BaseException as exc:  # reported below
            errors.append(exc)

    writer = threading.Thread(target=write)
    writer.start()
    seen = 0
    while writer.is_alive():
        doc = json.loads(retry_sharing(lambda: path.read_text(encoding="utf-8")))
        assert doc["n"] >= seen  # never a partial or older file
        seen = doc["n"]
    writer.join()
    assert not errors, errors
    assert json.loads(path.read_text(encoding="utf-8")) == {"n": 200}
