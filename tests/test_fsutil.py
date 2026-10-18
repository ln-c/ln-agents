"""File locks and reads that must behave the same on Linux, macOS and Windows."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from agentic_pipeline.fsutil import O_BINARY, lock, read_all, unlock


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
