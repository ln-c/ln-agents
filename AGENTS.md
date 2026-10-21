# Agent instructions — agentic-pipeline

This file is for the people and coding agents that change this repository: the `agentic_pipeline` package and the `ln-agents` binary. `ln-agents` never reads it, and the models a run calls do not load it as instructions. Two other files have similar names and different readers: [`agents/*.md`](agents/) are the role specifications of the pipeline's own agents, and [`LN-AGENTS.md`](LN-AGENTS.md) is the setup file of a project that runs `ln-agents` (see [Files at the root](docs/reference/repository-layout.md#files-at-the-root)). `CLAUDE.md` contains only an import of this file.

This repository's deterministic implementation remains authoritative. Agent specifications define bounded responsibilities; `pipeline/graph.yaml`, human-owned policy under `policy/`, schemas, and executable checks determine what may actually happen.

This is a policy-driven repository: do not infer implementation details from the README when source files, schemas, graph definitions, policies, or tests can verify them.

If a file is absent, do not claim it exists or invent its contents. Report the gap and propose a minimal change for human review.

## Repository integration map

| Concern | Canonical location | Rule |
|---|---|---|
| State machine and guarded transitions | `pipeline/graph.yaml` | Only the deterministic orchestrator may execute transitions. |
| Retry and spend budgets | `policy/budgets.yaml` | Model prompts cannot increase budgets. |
| Fast-path criteria | `policy/fast_path.yaml` | A fast path cannot bypass hard vetoes or human approval. |
| Hard vetoes | `policy/vetoes.yaml` | Fail closed; model output cannot clear a veto. |
| MCDA weights and thresholds | `policy/weights.yaml` | Deterministic scoring; changes require policy review. |
| Finding triage threshold | `policy/triage.yaml` | A model's probability is compared in code; a waived finding is shown at H1, never deleted. |
| Data classes | `policy/data_classes.yaml` | Determines what may be sent to each execution backend. |
| Escalation permissions | `policy/escalation.yaml` | Cloud escalation requires an allowed data class and explicit route. |
| Exception routes | `policy/routing.yaml` | Model classifications are suggestions until validated by deterministic rules. |
| Model access and assignment | `policy/model_registry.yaml` | Logical aliases only; no authority to change state or permissions. |
| Artifact contracts | `schemas/` | Validate all structured agent outputs before use. |
| Python implementation | `src/agentic_pipeline/` | Implement/validate graph, policy, registry and artifact invariants here. |
| Invariant tests | `tests/` | Tests and contracts are protected from implementation-agent writes. |
| Agent role specifications | `agents/*.md` | Role instructions, not a security boundary. |

## Cross-cutting invariants

1. `pipeline/graph.yaml` is the source of truth for legal states and transitions; this document and all model prompts are subordinate to it.
2. Every run is bound to a pinned Git SHA, context-manifest digest, policy/config versions, and artifact digests.
3. User declarations are not repository facts until verified. Missing or conflicting evidence is explicit and fails closed at required gates.
4. Design A and Design B use two different model families, bound in `policy/model_registry.yaml` (currently A = Claude Pro, B = Google AI Pro), and receive separate sessions and separate write roots. Neither may read the other's proposal before its own proposal is sealed. If the runtime cannot enforce this, independence is unmet and the run pauses or records the unmet requirement.
5. Model output is untrusted data. Validate schemas, artifact references, path allowlists, tool calls, and claims before use.
6. Filesystem permissions, network access, secret access, protected paths, and sandboxing must be enforced outside the model.
7. Build agents cannot modify protected tests, frozen contracts, policy, budgets, approvals, or release state.
8. Human approval is explicit, attributable, and bound to the exact artifact digest and target. Merge and deployment approvals are separate.
9. Models may explain, draft, critique, or provide bounded classifications. They cannot authorize transitions, change policy, approve releases, or override security vetoes.
10. Learning writes append-only provenance. `L2` runs only after a verified merge and records the exact merged SHA; an index refresh (not built yet) would require the same, and calibration changes require offline evaluation and human review.
11. Subscription quota exhaustion causes an explicit pause or registry-approved local fallback. No paid API fallback, credential-based billing substitution, or silent provider switch is allowed.

## Phase fidelity

Act only in the phase and role explicitly assigned by the orchestrator. Do not combine proposal generation, critique, synthesis, implementation, approval, and release authority in one invocation unless the deterministic workflow explicitly allows it.

### Design isolation

Invariant 4 sets the rule. In a design phase:

- Each proposal starts from the same sealed requirements/context snapshot and runs in an isolated session and write root.
- Do not read the other proposal or its critique until your own proposal is sealed.
- If isolation cannot be demonstrated by runtime controls, report `isolation_unverified`; do not claim independent multi-model review.

### Implementation

- Follow the accepted design, frozen contracts, and protected tests.
- Modify only assigned implementation paths.
- Never alter tests, contracts, policy, budgets, approvals, or release state to make a change pass.
- Run the repository's required checks: `uv sync`, `uv run pytest`, `uv run python -m agentic_pipeline validate`, `uv run ruff check .`, and `uv run ruff format --check .`, and for `rust/` (ln-agents) `cargo fmt --check`, `cargo clippy --all-targets --locked -- -D warnings`, and `cargo test --locked`, where the corresponding project configuration is present and commands are permitted.
- If dependencies, sandbox guarantees, test commands, or allowed write paths are unavailable, report a blocked result instead of guessing.

## Subscription and local execution policy

- Use only provider-supported subscription-backed workflows for Claude Pro and Google AI Pro. Do not use paid API endpoints, API keys, or usage-credit fallback.
- Use the local model bound in `policy/model_registry.yaml` (currently Qwen3.5 9B on llama.cpp) for tasks explicitly assigned to the local backend and within its quality/safety limits.
- Clef-flash is optional and allowed only while its free allowance is available. If unavailable or exhausted, use deterministic rules or pause.
- The local Clef-Flash on llama.cpp (the decision server on port 8081, bound in `policy/model_registry.yaml`) is required at A2J to judge critique findings; if it is not ready, the run pauses.
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

## Agent index

The pipeline's own agents, the specification of each and the states it covers are listed in [Agent specifications](docs/concepts/agent-specifications.md#the-agents).

## Model registry

Read [`policy/model_registry.yaml`](policy/model_registry.yaml). The registry assigns execution backends but does not grant permissions. `policy/budgets.yaml`, `policy/data_classes.yaml`, `policy/escalation.yaml`, `policy/vetoes.yaml`, and `pipeline/graph.yaml` remain authoritative for their respective controls.

`uv run python -m agentic_pipeline validate` validates the registry against [`schemas/model_registry.schema.json`](schemas/model_registry.schema.json) and cross-checks it with the graph; `tests/test_registry.py` pins those rules. [`README.md`](README.md) lists which states are built.
