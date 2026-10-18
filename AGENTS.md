# Agentic Pipeline — Agent Registry

This repository's deterministic implementation remains authoritative. Agent specifications define bounded responsibilities; `pipeline/graph.yaml`, human-owned policy under `policy/`, schemas, and executable checks determine what may actually happen.

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

## Agent index

| Agent | Specification | Existing pipeline stages |
|---|---|---|
| Grounding | [`agents/pipeline-grounding-agent.md`](agents/pipeline-grounding-agent.md) | START, G; context manifest and evidence |
| Design | [`agents/pipeline-design-agent.md`](agents/pipeline-design-agent.md) | A1–A4; independent proposals, critique, synthesis |
| Build | [`agents/pipeline-build-agent.md`](agents/pipeline-build-agent.md) | C1–E4; decomposition, contracts, tests, implementation |
| Exception Router | [`agents/pipeline-exception-router.md`](agents/pipeline-exception-router.md) | X; evidence-based exception classification and routing |
| Release | [`agents/pipeline-release-agent.md`](agents/pipeline-release-agent.md) | F1–RB; CI, merge, deploy authorization, rollback (`D2`, `RB` not built) |
| Learning | [`agents/pipeline-learning-agent.md`](agents/pipeline-learning-agent.md) | L1–L3, HR; provenance and gated evaluation |
| Human Authority | [`agents/pipeline-human-authority-agent.md`](agents/pipeline-human-authority-agent.md) | H1, DR, BENCH, CHILD; explicit human decisions |

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

## Model registry

Read [`policy/model_registry.yaml`](policy/model_registry.yaml). The registry assigns execution backends but does not grant permissions. `policy/budgets.yaml`, `policy/data_classes.yaml`, `policy/escalation.yaml`, `policy/vetoes.yaml`, and `pipeline/graph.yaml` remain authoritative for their respective controls.

`uv run python -m agentic_pipeline validate` validates the registry against [`schemas/model_registry.schema.json`](schemas/model_registry.schema.json) and cross-checks it with the graph; `tests/test_registry.py` pins those rules. [`README.md`](README.md) lists which states are built.
