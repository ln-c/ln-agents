"""Path rules: the class of a project file, decided by an ordered list in
policy/data_classes.yaml.

```yaml
path_rules:
  - {path: "docs/**", class: internal}
  - {path: "README.md", class: internal}
```

`artifact_defaults` gives a whole artifact one class (the source tree is `confidential`). A
path rule refines that for the files of the repository: the first rule whose `path` matches a
file gives it that class, and a file no rule matches keeps its artifact's class. Rules only
matter to a caller outside the host, which is sent a file only if its class is `cloud_allowed`.

A `path` is relative to the project root, with `/`. Each segment is a pattern: `*`, `?` and
`[...]` match inside one segment only, and case is significant. A segment that is exactly `**`
matches any number of segments, none included, so `docs/**` covers `docs` and everything below
it, and `**/*.md` every Markdown file. Nothing here reads the file system or the MCP SDK.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from fnmatch import fnmatchcase
from typing import Any

MAX_PATTERN_SEGMENTS = 32


@dataclass(frozen=True, slots=True)
class PathRule:
    path: str
    data_class: str
    segments: tuple[str, ...]
    folded: tuple[str, ...]  # the same, case-folded, for the stricter reading of a path

    def matches(self, parts: Sequence[str], fold: bool = False) -> bool:
        """Whether the path `parts` is covered. `fold` ignores case (the caller folds `parts`)."""
        segments = self.folded if fold else self.segments
        return len(segments) in _states(segments, parts)

    def reaches(self, parts: Sequence[str]) -> bool:
        """Whether `parts`, or some path below it, may be covered, ignoring case. A test for
        pruning: it never says no when the answer could be yes."""
        return bool(_states(self.folded, fold_parts(parts)))


def fold_parts(parts: Sequence[str]) -> tuple[str, ...]:
    return tuple(p.casefold() for p in parts)


def _states(segments: tuple[str, ...], parts: Sequence[str]) -> set[int]:
    """The positions in `segments` the path `parts` can have reached: `len(segments)` in the
    result means the whole pattern matched, any position means a path that goes on below
    `parts` may still match."""

    def closure(states: set[int]) -> set[int]:
        out, todo = set(states), list(states)
        while todo:
            i = todo.pop()
            if i < len(segments) and segments[i] == "**" and i + 1 not in out:
                out.add(i + 1)
                todo.append(i + 1)
        return out

    states = closure({0})
    for part in parts:
        nxt: set[int] = set()
        for i in states:
            if i >= len(segments):
                continue
            if segments[i] == "**":
                nxt.add(i)
            elif fnmatchcase(part, segments[i]):
                nxt.add(i + 1)
        states = closure(nxt)
        if not states:
            break
    return states


def first_class(rules: Sequence[PathRule], parts: Sequence[str], fold: bool = False) -> str | None:
    """The class of the first rule that covers `parts`, or None."""
    probe = fold_parts(parts) if fold else tuple(parts)
    for rule in rules:
        if rule.matches(probe, fold):
            return rule.data_class
    return None


def _pattern_problem(path: Any) -> str | None:
    if not isinstance(path, str) or not path:
        return "must be a non-empty string"
    if "\x00" in path or "\\" in path:
        return f"{path!r} must use forward slashes and no NUL"
    if path.startswith("/") or re.match(r"^[A-Za-z]:", path):
        return f"{path!r} must be relative to the project root"
    segments = path.split("/")
    if len(segments) > MAX_PATTERN_SEGMENTS:
        return f"{path!r} has more than {MAX_PATTERN_SEGMENTS} segments"
    if any(s == "" for s in segments):
        return f"{path!r} has an empty segment (a trailing or doubled /)"
    if any(s in (".", "..") for s in segments):
        return f"{path!r} has a . or .. segment"
    if any("**" in s and s != "**" for s in segments):
        return f"{path!r}: ** is a whole segment"
    return None


def check_path_rules(doc: Mapping[str, Any]) -> list[str]:
    """The problems with `path_rules` in a data-classes document (none when it has no rules)."""
    rules = doc.get("path_rules")
    if rules is None:
        return []
    if not isinstance(rules, list):
        return ["data_classes: path_rules must be a list"]
    classes = doc.get("classes") or {}
    errors: list[str] = []
    seen: set[str] = set()
    for i, rule in enumerate(rules):
        where = f"data_classes: path_rules[{i}]"
        if not isinstance(rule, Mapping) or set(rule) != {"path", "class"}:
            errors.append(f"{where} must be a mapping with exactly path and class")
            continue
        path, cls = rule["path"], rule["class"]
        if not isinstance(cls, str) or cls not in classes:
            errors.append(f"{where}.class {cls!r} is not a class of this file")
        if problem := _pattern_problem(path):
            errors.append(f"{where}.path {problem}")
        elif path in seen:
            errors.append(f"{where}.path {path!r} repeats an earlier rule, which wins")
        else:
            seen.add(path)
    return errors


def parse_path_rules(doc: Mapping[str, Any]) -> tuple[PathRule, ...]:
    """The rules of a data-classes document, in order. `ValueError` if `check_path_rules`
    finds a problem, so a caller never serves files under rules it cannot read."""
    if errors := check_path_rules(doc):
        raise ValueError("; ".join(errors))
    return tuple(
        PathRule(
            r["path"],
            r["class"],
            tuple(r["path"].split("/")),
            tuple(s.casefold() for s in r["path"].split("/")),
        )
        for r in doc.get("path_rules") or []
    )
