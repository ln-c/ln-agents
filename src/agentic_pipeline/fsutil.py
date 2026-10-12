"""Crash-safe file publication shared by manifests and run state."""

from __future__ import annotations

import json
import os
import uuid
from collections.abc import Mapping
from pathlib import Path
from typing import Any


def fsync_dir(directory: Path) -> None:
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
