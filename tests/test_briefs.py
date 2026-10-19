"""The brief of an agent spec: extraction, and that every spec in the repository has one."""

from __future__ import annotations

from pathlib import Path

import pytest

from agentic_pipeline.briefs import BEGIN, END, MAX_BRIEF_BYTES, BriefError, extract
from agentic_pipeline.dispatch import brief_spec

ROOT = Path(__file__).resolve().parents[1]
SPECS = sorted((ROOT / "agents").glob("*.md"))


def spec(brief: str = "- **Role:** does a thing.") -> str:
    return f"# Agent\n\n{BEGIN}\n{brief}\n{END}\n\n## Mission\n\nEverything else.\n"


def test_the_brief_is_what_lies_between_the_markers_trimmed() -> None:
    assert extract(spec("\n  - **Role:** does a thing.  \n\n")) == "- **Role:** does a thing."


def test_the_rest_of_the_file_is_not_part_of_it() -> None:
    brief = extract(spec())
    assert "Mission" not in brief and "Everything else" not in brief and "# Agent" not in brief


@pytest.mark.parametrize(
    ("text", "why"),
    [
        ("# Agent\n\n## Mission\n", "has no brief"),
        (f"{BEGIN}\nx\n", "exactly one brief"),
        (f"x\n{END}\n", "exactly one brief"),
        (f"{BEGIN}\nx\n{END}\n{BEGIN}\ny\n{END}\n", "exactly one brief"),
        (f"{END}\nx\n{BEGIN}\n", "ends before it begins"),
        (spec("   \n"), "empty brief"),
        (spec("x" * (MAX_BRIEF_BYTES + 1)), f"the most is {MAX_BRIEF_BYTES}"),
    ],
)
def test_a_missing_doubled_reversed_empty_or_long_brief_is_refused(text: str, why: str) -> None:
    with pytest.raises(BriefError, match=why):
        extract(text)


def test_the_limit_counts_bytes_not_characters() -> None:
    assert extract(spec("a" * MAX_BRIEF_BYTES)) == "a" * MAX_BRIEF_BYTES
    with pytest.raises(BriefError, match="bytes"):
        extract(spec("é" * (MAX_BRIEF_BYTES // 2 + 1)))


def test_a_brief_reference_carries_the_digest_of_the_whole_file() -> None:
    text = spec()
    ref = brief_spec("agents/x.md", text)
    assert ref.brief and ref.text == "- **Role:** does a thing." and ref.path == "agents/x.md"
    other = brief_spec("agents/x.md", text.replace("Everything else.", "Something else."))
    assert ref.digest != other.digest and ref.text == other.text, (
        "editing the spec outside the brief changes the digest, so the reference never goes stale"
    )


@pytest.mark.parametrize("path", SPECS, ids=lambda p: p.name)
def test_every_spec_in_the_repository_has_a_brief_that_names_its_role(path: Path) -> None:
    brief = extract(path.read_text(encoding="utf-8"))
    assert "**Role:**" in brief and "**Never:**" in brief, "a brief says what the agent may not do"
    assert len(brief.encode("utf-8")) <= MAX_BRIEF_BYTES


def test_the_briefs_are_a_small_fraction_of_the_specs() -> None:
    whole = sum(len(p.read_text(encoding="utf-8")) for p in SPECS)
    briefs = sum(len(extract(p.read_text(encoding="utf-8"))) for p in SPECS)
    assert briefs * 10 < whole, "what the coordinator reads is a tenth of what the files hold"
