"""Crash-safe file publication and file locks shared by manifests, run state and provenance.

Everything here works on Linux, macOS and Windows. On POSIX a lock is `flock` on the whole
file. On Windows it is `msvcrt.locking` on one byte far past any data: Windows byte-range locks
are mandatory, so locking real bytes would make readers of the file fail while it is held.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any

WINDOWS = os.name == "nt"
# Binary mode for os.open: Windows otherwise translates "\n" in low-level writes.
O_BINARY: int = getattr(os, "O_BINARY", 0)
_WINDOWS_LOCK_OFFSET = 2**31 - 2  # one byte no file of ours ever reaches
_LOCK_POLL_SECONDS = 0.01


def lock(fd: int, *, blocking: bool) -> None:
    """Take an exclusive lock on an open file; non-blocking raises BlockingIOError if held."""
    if not WINDOWS:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
        return
    import msvcrt

    while True:
        os.lseek(fd, _WINDOWS_LOCK_OFFSET, os.SEEK_SET)
        try:
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
            return
        except OSError as exc:
            if not blocking:
                raise BlockingIOError(str(exc)) from exc
        time.sleep(_LOCK_POLL_SECONDS)


def unlock(fd: int) -> None:
    """Release a lock taken by `lock`. Closing the file also releases it on POSIX; on Windows
    the release at close may be delayed, so callers unlock explicitly before closing."""
    if not WINDOWS:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_UN)
        return
    import msvcrt

    os.lseek(fd, _WINDOWS_LOCK_OFFSET, os.SEEK_SET)
    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)


def read_all(fd: int) -> bytes:
    """The whole file from offset 0 (a portable `os.pread(fd, size, 0)`); moves the offset."""
    os.lseek(fd, 0, os.SEEK_SET)
    chunks: list[bytes] = []
    while chunk := os.read(fd, 1 << 20):
        chunks.append(chunk)
    return b"".join(chunks)


def fsync_dir(directory: Path) -> None:
    """Make a directory entry durable. Windows cannot open a directory for fsync, and NTFS
    journals its metadata, so there it is a no-op."""
    if WINDOWS:
        return
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_write_json(path: Path, document: Mapping[str, Any]) -> None:
    """Write `document` so readers see the old file or the new one, never a partial write.

    Temp file in the same directory, fsync, `os.replace`, then fsync of the directory.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp.{uuid.uuid4().hex}")
    try:
        with tmp.open("x", encoding="utf-8") as fh:
            json.dump(document, fh, sort_keys=True, indent=2)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)
    fsync_dir(path.parent)
