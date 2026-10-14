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
        "Field rules:\n"
        "- `id`: a short unique id you choose for this proposal.\n"
        "- `author_model`: the name of the model writing it.\n"
        "- `manifest_hash`: copy the `hash` value from the pinned context manifest input "
        "exactly.\n"
        "- `topology.components`: at least one component, each with a `name`.\n"
        "- `dependencies`: every third-party package this design would add, each with `name`, "
        "`ecosystem` (for example `pypi`), the exact `version` you intend, and a `purpose`. "
        "Use an empty list if it adds none. Leave the field out only if you cannot say; the "
        "dependency veto cannot clear a design that omits it.\n"
        "- `data_flows`: every flow of data from this design to a destination, each with an "
        "`id`, the `data_class` of the data (a class from the context manifest's data "
        "classification: public, internal, confidential, restricted or unknown) and the "
        "`destination` kind: `local` (this host), `internal_service`, `cloud_service` or "
        "`third_party`. Use an empty list if the design sends no data anywhere. Leave the field "
        "out only if you cannot say; the data-class veto cannot clear a design that omits it.\n"
        "- `synthesized`: false for an independent proposal; true only for a synthesis, "
        "which must also give `sources` mapping each component name to the id of the input "
        "proposal it came from (use only ids that appear in the proposal inputs)."
    ),
    "critique": (
        "Review the proposal on all nine dimensions: (1) explicit and implied context "
        "manifest constraints; (2) API and data-contract satisfaction, versioning and "
        "compatibility; (3) security boundaries, access control, threat exposure, secrets "
        "handling and data classification; (4) logical consistency and architectural "
        "feasibility; (5) concurrency, consistency, idempotency, failure handling and "
        "recovery; (6) observability, operability and diagnosability; (7) dependency and "
        "cross-service boundary changes; (8) acceptance criteria and testability; (9) "
        "assumptions, missing evidence and unbounded risks.\n"
        "Severity values: `blocking` is a substantiated violation of the context manifest or "
        "a required contract, a credible security vulnerability, or a material logical "
        "contradiction that prevents a mandatory requirement from being met; `non_blocking` "
        "is a weakness, inefficiency or trade-off that violates no mandatory constraint; "
        "`informational` is an uncertainty or observation with no established defect. Sparse "
        "or missing detail is not by itself a blocking finding.\n"
        "Field rules:\n"
        "- `target_proposal_id`: copy the `id` of the one proposal you were given to review.\n"
        "- `manifest_hash`: copy the `hash` value from the pinned context manifest input "
        "exactly.\n"
        "- `findings`: every finding has a severity of `blocking`, `non_blocking` or "
        "`informational`. A `blocking` finding must also name the `proposal_id`, `subject`, "
        "`impact`, the constraint `violated`, at least one piece of `evidence`, and a "
        "`remediation`; a severity label alone is not evidence.\n"
        "- `counts`: must equal the number of findings of each severity.\n"
        "- `review_complete`: true only if you reviewed every required dimension.\n"
        "- `scores`: for each of security, reliability, scalability, maintainability and cost, "
        "give an integer `score` from 0 to 4 and the `evidence` for it (a concrete fact from "
        "the proposal). 0 means the proposal fails the criterion, 2 means it meets it with "
        "notable weaknesses, 4 means it meets it with no weakness you can find. Score each "
        "criterion on this proposal alone, never relative to another. Omit a criterion you "
        "cannot support with evidence; an unsupported score is worse than none."
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
        f"{guidance}\n\n"
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
