"""The brief of an agent specification: the few lines a coordinator reads in place of the file.

Each `agents/*.md` is long, and what an agent's role is, where it stops, and what it hands on
fits in a page. A brief is that page, kept inside the spec between two markers and owned like the
rest of `agents/` (CODEOWNERS): it changes only through a reviewed commit. No model at run time
ever writes or updates one, so what a coordinator is told about a role cannot be something a
model said about itself. Code only extracts it; registry rule R21 keeps every spec's brief
present, single and short.

A stage's own model still gets the whole spec (it is that model's role); the brief is for the
models that talk *about* the agents, the coordinator and the authority.
"""

from __future__ import annotations

BEGIN = "<!-- ln-agents:brief begin -->"
END = "<!-- ln-agents:brief end -->"
MAX_BRIEF_BYTES = 2048


class BriefError(ValueError):
    """The spec has no usable brief (none, more than one, empty, or too long); the message reads
    after the spec's name."""


def extract(text: str) -> str:
    """The brief between the markers, trimmed; raises BriefError if there is not exactly one."""
    begins, ends = text.count(BEGIN), text.count(END)
    if begins == 0 and ends == 0:
        raise BriefError(f"has no brief: put it between {BEGIN} and {END}")
    if begins != 1 or ends != 1:
        raise BriefError(f"must hold exactly one brief, not {begins} begin and {ends} end markers")
    start = text.index(BEGIN) + len(BEGIN)
    stop = text.index(END)
    if stop < start:
        raise BriefError("has a brief that ends before it begins")
    brief = text[start:stop].strip()
    if not brief:
        raise BriefError("has an empty brief")
    size = len(brief.encode("utf-8"))
    if size > MAX_BRIEF_BYTES:
        raise BriefError(f"has a brief of {size} bytes; the most is {MAX_BRIEF_BYTES}")
    return brief
