"""Text edits to `policy/model_registry.yaml` that leave every comment and the layout alone.

The registry is human-owned and heavily commented, so it is edited as text: PyYAML's node
marks say exactly where a value or an entry starts and ends. A round trip through a YAML
dumper would drop the comments. Each function returns new text and nothing else; the caller
(`registry_changes`) parses it and checks that it holds exactly the registry it meant to write
before anything is saved, so a wrong edit is refused, never written.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from typing import Any

import yaml

BARE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class EditError(Exception):
    """The edit cannot be made as text; the file is left as it is."""


def _root(text: str) -> yaml.MappingNode:
    node = yaml.compose(text, Loader=yaml.SafeLoader)
    if not isinstance(node, yaml.MappingNode):
        raise EditError("the registry is not a YAML mapping")
    return node


def _child(node: yaml.Node, key: str) -> tuple[yaml.Node, yaml.Node]:
    if isinstance(node, yaml.MappingNode):
        for k, v in node.value:
            if isinstance(k, yaml.ScalarNode) and k.value == key:
                return k, v
    raise EditError(f"no key {key!r}")


def _last_content_end(node: yaml.Node) -> int:
    """Where the last value of `node` ends; a block collection's own end mark can run on past
    the comments that follow it."""
    if isinstance(node, yaml.ScalarNode) or node.flow_style:
        return node.end_mark.index
    if isinstance(node, yaml.MappingNode):
        return max(_last_content_end(v) for _, v in node.value)
    return max(_last_content_end(item) for item in node.value)


def _line_start(text: str, index: int) -> int:
    return text.rfind("\n", 0, index) + 1


def _line_end(text: str, index: int) -> int:
    """The index of the newline that ends the line `index` is on (or the end of the text)."""
    end = text.find("\n", index)
    return len(text) if end == -1 else end


def flow(value: str | Sequence[str]) -> str:
    """A slot value as YAML: a bare scalar, or a flow list `[a, b]`."""
    if isinstance(value, str):
        return value
    return "[" + ", ".join(value) + "]"


def set_value(text: str, path: Sequence[str], value: str | Sequence[str]) -> str:
    """Replace the scalar or flow list at `path` (keys from the top of the registry)."""
    node: yaml.Node = _root(text)
    where = ".".join(path)
    try:
        for key in path:
            _, node = _child(node, key)
    except EditError as exc:
        raise EditError(f"{where}: {exc}") from exc
    scalar = isinstance(node, yaml.ScalarNode)
    inline = isinstance(node, yaml.SequenceNode) and node.flow_style
    if not (scalar or inline):
        raise EditError(
            f"{where} is written as a block, which the editor does not rewrite; "
            "edit policy/model_registry.yaml by hand"
        )
    return text[: node.start_mark.index] + flow(value) + text[node.end_mark.index :]


def set_slot(text: str, path: Sequence[str], value: str | Sequence[str]) -> str:
    """Replace the value at `agents.<path>`."""
    return set_value(text, ["agents", *path], value)


def _scalar(value: str) -> str:
    return value if BARE.match(value) else json.dumps(value)


def model_entry(name: str, entry: Mapping[str, Any]) -> str:
    """One catalog line, in the flow style the registry uses for models without comments."""
    fields = [
        f"provider: {_scalar(entry['provider'])}",
        f"model_id: {json.dumps(entry['model_id'])}",
        f"alias: {json.dumps(entry['alias'])}",
        f"family: {_scalar(entry['family'])}",
        f"capabilities: [{', '.join(entry['capabilities'])}]",
    ]
    return f"  {name}: {{{', '.join(fields)}}}"


def add_model(text: str, name: str, entry: Mapping[str, Any]) -> str:
    """Append a model to the catalog, on a line of its own after the last entry."""
    if not BARE.match(name):
        raise EditError(f"{name!r} is not a usable model name")
    _, models = _child(_root(text), "models")
    if not isinstance(models, yaml.MappingNode) or not models.value:
        raise EditError("the catalog is empty or not a mapping")
    insert_at = _line_end(text, _last_content_end(models.value[-1][1])) + 1
    return text[:insert_at] + model_entry(name, entry) + "\n" + text[insert_at:]


def remove_model(text: str, name: str) -> str:
    """Delete a catalog entry (its own lines only; comments above it stay)."""
    _, models = _child(_root(text), "models")
    for key, value in models.value:
        if isinstance(key, yaml.ScalarNode) and key.value == name:
            start = _line_start(text, key.start_mark.index)
            end = _line_end(text, _last_content_end(value)) + 1
            return text[:start] + text[end:]
    raise EditError(f"no model {name!r} in the catalog")


PLAIN = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_./-]*$")


def _block(node: yaml.Node, where: str) -> yaml.MappingNode:
    if not isinstance(node, yaml.MappingNode) or node.flow_style or not node.value:
        raise EditError(
            f"{where} is not written as a block mapping, which the editor does not extend; "
            "edit policy/model_registry.yaml by hand"
        )
    return node


def _after(text: str, node: yaml.MappingNode, lines: str) -> str:
    """`lines` (each led by a newline) inserted right after the last value of `node`."""
    at = _line_end(text, _last_content_end(node))
    return text[:at] + lines + text[at:]


def add_key(text: str, path: Sequence[str], key: str, value: str | Sequence[str]) -> str:
    """Add `key: value` as the last key of the block mapping at `path`, indented like its keys."""
    if not BARE.match(key):
        raise EditError(f"{key!r} is not a usable key")
    node: yaml.Node = _root(text)
    where = ".".join(path)
    try:
        for step in path:
            _, node = _child(node, step)
    except EditError as exc:
        raise EditError(f"{where}: {exc}") from exc
    entry = _block(node, where)
    if any(isinstance(k, yaml.ScalarNode) and k.value == key for k, _ in entry.value):
        raise EditError(f"{where} already has {key!r}")
    indent = " " * entry.value[0][0].start_mark.column
    return _after(text, entry, f"\n{indent}{key}: {flow(value)}")


def add_agent(text: str, key: str, spec: str, model: str) -> str:
    """Add `key: {spec, primary: model}` as the last entry under `agents`, after a blank line."""
    if not BARE.match(key):
        raise EditError(f"{key!r} is not a usable agent name")
    _, agents = _child(_root(text), "agents")
    entries = _block(agents, "agents")
    if any(isinstance(k, yaml.ScalarNode) and k.value == key for k, _ in entries.value):
        raise EditError(f"agents already has {key!r}")
    indent = " " * entries.value[0][0].start_mark.column
    path = spec if PLAIN.match(spec) else json.dumps(spec)
    lines = f"\n\n{indent}{key}:\n{indent}  spec: {path}\n{indent}  primary: {flow(model)}"
    return _after(text, entries, lines)
