"""Text edits to the registry: comments and layout survive, and unsupported shapes are refused."""

from __future__ import annotations

import difflib
from pathlib import Path

import pytest

from agentic_pipeline import registry_edit
from agentic_pipeline.graph import loads_yaml

ROOT = Path(__file__).resolve().parents[1]
TEXT = (ROOT / "policy" / "model_registry.yaml").read_text(encoding="utf-8")
ENTRY = {
    "provider": "google_ai_pro",
    "model_id": "gemini-9-new",
    "alias": "Gemini 9 (new)",
    "family": "gemini",
    "capabilities": ["design_reasoning", "coordination"],
}


def parsed(text: str) -> dict:
    return loads_yaml(text.encode("utf-8"))


def comments(text: str) -> list[str]:
    return [line for line in text.splitlines() if line.lstrip().startswith("#")]


def only_lines_differ(old: str, new: str) -> tuple[list[str], list[str]]:
    a, b = old.splitlines(), new.splitlines()
    return [x for x in a if x not in b], [x for x in b if x not in a]


def test_a_scalar_slot_is_replaced_in_place() -> None:
    new = registry_edit.set_slot(TEXT, ["design", "proposal_b", "primary"], "gemini_4_argon_high")
    assert parsed(new)["agents"]["design"]["proposal_b"]["primary"] == "gemini_4_argon_high"
    removed, added = only_lines_differ(TEXT, new)
    assert removed == ["      primary: gemini_4_argon_medium"]
    assert added == ["      primary: gemini_4_argon_high"]
    assert comments(new) == comments(TEXT)


def test_a_flow_list_slot_is_replaced_in_place() -> None:
    value = ["gemini_3_8_flash_high", "claude_cli_default", "qwen3_5_9b"]
    new = registry_edit.set_slot(TEXT, ["human_authority", "model_assistance"], value)
    assert parsed(new)["agents"]["human_authority"]["model_assistance"] == value
    assert only_lines_differ(TEXT, new)[1] == [
        "    model_assistance: [gemini_3_8_flash_high, claude_cli_default, qwen3_5_9b]"
    ]


def test_a_list_can_shrink_to_one_and_a_trailing_comment_stays() -> None:
    text = "agents:\n  a:\n    model_assistance: [x, y]  # kept\n"
    new = registry_edit.set_slot(text, ["a", "model_assistance"], ["x"])
    assert new == "agents:\n  a:\n    model_assistance: [x]  # kept\n"


def test_capabilities_of_a_catalog_entry_are_replaced_in_its_flow_mapping() -> None:
    new = registry_edit.set_value(
        TEXT, ["models", "gemini_4_argon_high", "capabilities"], ["design_reasoning"]
    )
    assert parsed(new)["models"]["gemini_4_argon_high"]["capabilities"] == ["design_reasoning"]
    removed, added = only_lines_differ(TEXT, new)
    assert len(removed) == len(added) == 1 and "gemini_4_argon_high" in added[0]


def test_a_block_list_is_refused_not_mangled() -> None:
    text = "agents:\n  a:\n    model_assistance:\n      - x\n      - y\n"
    with pytest.raises(registry_edit.EditError, match="block"):
        registry_edit.set_slot(text, ["a", "model_assistance"], ["z"])


def test_a_slot_that_is_not_there_is_refused() -> None:
    with pytest.raises(registry_edit.EditError, match="no key 'nope'"):
        registry_edit.set_slot(TEXT, ["design", "nope", "primary"], "x")


def test_a_model_is_appended_after_the_last_entry_and_before_the_next_section() -> None:
    new = registry_edit.add_model(TEXT, "gemini_9_new", ENTRY)
    assert parsed(new)["models"]["gemini_9_new"] == ENTRY
    assert list(parsed(new)["models"])[-1] == "gemini_9_new"
    removed, added = only_lines_differ(TEXT, new)
    assert removed == [] and len(added) == 1
    lines = new.splitlines()
    index = lines.index(added[0])
    assert lines[index - 1].lstrip().startswith("gpt_oss_120b_medium:")
    assert lines[index + 1] == "" and lines[index + 2] == "agents:"
    assert comments(new) == comments(TEXT)


def test_a_model_id_and_alias_that_need_quotes_are_quoted() -> None:
    entry = {**ENTRY, "model_id": "meta/llama: 3", "alias": 'say "hi"'}
    new = registry_edit.add_model(TEXT, "llama", entry)
    assert parsed(new)["models"]["llama"] == entry


def test_an_unusable_model_name_is_refused() -> None:
    with pytest.raises(registry_edit.EditError, match="not a usable model name"):
        registry_edit.add_model(TEXT, "bad name: {", ENTRY)


@pytest.mark.parametrize(
    "name", ["gemini_4_argon_low", "claude_cli_default", "gpt_oss_120b_medium"]
)
def test_removing_a_model_deletes_only_its_own_lines(name: str) -> None:
    new = registry_edit.remove_model(TEXT, name)
    expected = parsed(TEXT)
    del expected["models"][name]
    assert parsed(new) == expected
    removed, added = only_lines_differ(TEXT, new)
    assert added == [] and any(name in line for line in removed)
    assert comments(new) == comments(TEXT)  # a comment above an entry may describe a group


def test_removing_a_block_entry_takes_all_its_lines_and_no_other() -> None:
    new = registry_edit.remove_model(TEXT, "claude_cli_default")
    gone = [
        line[1:].strip().split(":")[0]
        for line in difflib.unified_diff(TEXT.splitlines(), new.splitlines(), lineterm="", n=0)
        if line.startswith("-") and not line.startswith("---")
    ]
    assert gone == [
        "claude_cli_default",
        "provider",
        "model_id",
        "alias",
        "family",
        "capabilities",
    ]


def test_removing_a_model_that_is_not_there_is_refused() -> None:
    with pytest.raises(registry_edit.EditError, match="no model 'ghost'"):
        registry_edit.remove_model(TEXT, "ghost")
