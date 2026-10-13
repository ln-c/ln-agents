"""Task text for operations whose registry entry sets `output_schema`.

The model is asked for exactly one JSON object matching a repo schema. The values it must
copy (the context hash, the proposal ids) are in the sealed inputs embedded below the task,
and the deterministic gates verify the copies, so a careless reply costs one budgeted retry
and is never repaired or reshaped here.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path

from agentic_pipeline.schemas import SCHEMA_DIR

PREAMBLE = (
    "Reply with exactly one JSON object and nothing else: no prose, no Markdown, and no "
    "code fence. It must validate against the JSON Schema below (draft-07)."
)

GUIDANCE: Mapping[str, str] = {
    "proposal": (
        "- `id`: a short unique id you choose for this proposal.\n"
        "- `author_model`: the name of the model writing it.\n"
        "- `manifest_hash`: copy the `hash` value from the pinned context manifest input "
        "exactly.\n"
        "- `topology.components`: at least one component, each with a `name`.\n"
        "- `synthesized`: false for an independent proposal; true only for a synthesis, "
        "which must also give `sources` mapping each component name to the id of the input "
        "proposal it came from (use only ids that appear in the proposal inputs)."
    ),
    "critique": (
        "- `target_proposal_id`: copy the `id` of the one proposal you were given to review.\n"
        "- `manifest_hash`: copy the `hash` value from the pinned context manifest input "
        "exactly.\n"
        "- `findings`: every finding has a severity of `blocking`, `non_blocking` or "
        "`informational`. A `blocking` finding must also name the `proposal_id`, `subject`, "
        "`impact`, the constraint `violated`, at least one piece of `evidence`, and a "
        "`remediation`; a severity label alone is not evidence.\n"
        "- `counts`: must equal the number of findings of each severity.\n"
        "- `review_complete`: true only if you reviewed every required dimension."
    ),
}


def load_schema_text(name: str, directory: Path = SCHEMA_DIR) -> str:
    schema = json.loads((directory / f"{name}.schema.json").read_text(encoding="utf-8"))
    return json.dumps(schema, indent=2, sort_keys=True)


def json_task(role: str, stage: str, operation: str, schema: str) -> str:
    guidance = GUIDANCE.get(schema, "")
    return (
        f"Act as the {role} for pipeline stage {stage}, operation {operation}.\n"
        f"{PREAMBLE}\n\n"
        f"Field rules:\n{guidance}\n\n"
        f"JSON Schema ({schema}):\n{load_schema_text(schema)}"
    )


def extract_json_object(text: str) -> dict[str, object] | None:
    """The reply as a JSON object, tolerating one surrounding code fence; else None."""
    body = text.strip()
    if body.startswith("```"):
        lines = body.splitlines()
        if len(lines) >= 2 and lines[-1].strip() == "```":
            body = "\n".join(lines[1:-1]).strip()
    try:
        doc = json.loads(body)
    except ValueError:
        return None
    return doc if isinstance(doc, dict) else None
