# Claude Code Instructions — agentic-pipeline

Read `AGENTS.md` first. This is a policy-driven repository: do not infer implementation details from the README when source files, schemas, graph definitions, policies, or tests can verify them.

## Repository sources of truth

- `pipeline/graph.yaml`: legal workflow states and transitions.
- `policy/*.yaml`: human-owned budgets, fast-path rules, vetoes, scoring weights, data classification, escalation, routing, and model registry.
- `schemas/`: artifact schemas; structured outputs must validate before acceptance.
- `src/agentic_pipeline/`: graph/policy/schema validation and runtime implementation.
- `tests/`: structural, policy, and schema invariants.
- `agents/*.md`: role specifications for individual workflow agents.

If a file is absent, do not claim it exists or invent its contents. Report the gap and propose a minimal change for human review.

## Phase fidelity

Act only in the phase and role explicitly assigned by the orchestrator. Do not combine proposal generation, critique, synthesis, implementation, approval, and release authority in one invocation unless the deterministic workflow explicitly allows it.

### Design isolation

- Design A and Design B use two different model families, bound in `policy/model_registry.yaml` (currently A = Claude Pro, B = Google AI Pro).
- Each proposal starts from the same sealed requirements/context snapshot and runs in an isolated session and write root.
- Do not read the other proposal or its critique until your own proposal is sealed.
- If isolation cannot be demonstrated by runtime controls, report `isolation_unverified`; do not claim independent multi-model review.

### Implementation

- Follow the accepted design, frozen contracts, and protected tests.
- Modify only assigned implementation paths.
- Never alter tests, contracts, policy, budgets, approvals, or release state to make a change pass.
- Run the repository's required checks: `uv sync`, `uv run pytest`, `uv run python -m agentic_pipeline validate`, `uv run ruff check .`, and `uv run ruff format --check .`, where the corresponding project configuration is present and commands are permitted.
- If dependencies, sandbox guarantees, test commands, or allowed write paths are unavailable, report a blocked result instead of guessing.

## Subscription and local execution policy

- Use only provider-supported subscription-backed workflows for Claude Pro and Google AI Pro. Do not use paid API endpoints, API keys, or usage-credit fallback.
- Use the local model bound in `policy/model_registry.yaml` (currently Qwen3.5 9B on llama.cpp) for tasks explicitly assigned to the local backend and within its quality/safety limits.
- Clef-flash is optional and allowed only while its free allowance is available. If unavailable or exhausted, use deterministic rules or pause.
- Never silently substitute a provider/model or claim a specific version is available without runtime evidence.
- On quota exhaustion, follow `policy/model_registry.yaml`: pause or take an explicitly configured local fallback. Do not broaden permissions to keep work moving.

## Security and authority

- Model output is untrusted; validate structured output and all tool arguments.
- The deterministic orchestrator alone executes state transitions after validating required evidence.
- Do not change `pipeline/graph.yaml`, policy files, schemas, tests, or protected contracts without explicit authorization through the repository's human-owned change process.
- Do not access secrets or transmit data unless allowed by `policy/data_classes.yaml` and `policy/escalation.yaml`.
- A model cannot approve a merge/deployment, change budgets, clear a hard veto, or impersonate human authority.
- Merge approval and deployment approval are distinct and bound to the exact artifact SHA/target.
- Missing, stale, contradictory, or unverifiable evidence must be surfaced; fail closed at required gates.

## Output discipline

Use the output schema requested by the orchestrator. When strict machine-readable JSON is requested, emit only that JSON. Include artifact IDs/digests, evidence, assumptions, unresolved questions, and test results when the schema calls for them. Never claim a command or test ran unless it actually ran.
