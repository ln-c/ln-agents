"""Compile and run artifact schemas with fastjsonschema (schemas compiled once, cached)."""

from __future__ import annotations

import json
from collections.abc import Callable
from functools import cache
from pathlib import Path
from typing import Any

import fastjsonschema

from agentic_pipeline.graph import REPO_ROOT

SCHEMA_DIR = REPO_ROOT / "schemas"
ValidationError = fastjsonschema.JsonSchemaException


def schema_names(directory: Path = SCHEMA_DIR) -> list[str]:
    return sorted(p.name.removesuffix(".schema.json") for p in directory.glob("*.schema.json"))


@cache
def validator(name: str, directory: Path = SCHEMA_DIR) -> Callable[[Any], Any]:
    schema = json.loads((directory / f"{name}.schema.json").read_text(encoding="utf-8"))
    return fastjsonschema.compile(schema)


def validate(name: str, document: Any) -> Any:
    """Validate `document` against schema `name`; raises ValidationError on failure."""
    return validator(name)(document)
