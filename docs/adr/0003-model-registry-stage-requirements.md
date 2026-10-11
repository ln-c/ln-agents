# 0003. Model Registry Stage Requirements and Provider Assignment

Date: 2026-10-12
Status: Proposed

## Context

The model registry overlay (`policy/model_registry.yaml`) and `pipeline/graph.yaml` disagreed on several points, and the registry had no per-stage operation scopes for the runtime to enforce. Each item below is a human-owned decision; nothing here takes effect until @ln-c approves it.

## Decisions

### 1. Design A = Claude Pro, Design B = Google AI Pro
`AGENTS.md` (invariant 4), `CLAUDE.md`, and the registry all assign A to Claude and B to Google. Only the free-text `doc:` on graph state `A1` said the reverse. The graph doc string is corrected to match; no state or edge changes.

### 2. Build primary is `qwen_local`
Graph state `E1` is "Pi + Qwen3 8B". The registry previously named `claude_pro` primary with Qwen as fallback. Cloud models now reach build only through the graph's `E3 → E4` escalation path, which is disabled in `stage_requirements` until `policy/escalation.yaml` targets map to registry providers.

### 3. A3 synthesis keeps `deterministic_orchestrator` as owner
The orchestrator owns synthesis; one model from `model_assistance` (Claude first, then Google) drafts it. `A4` validates the result deterministically.

### 4. Retry budgets are consumed per graph edge
`graph.yaml` consumes an edge's budget each time the edge is taken. A stage with several model invocations (A1, A2) still consumes one unit per entering edge. Per-invocation records exist only for provenance correlation.

### 5. Unrouted dispatch failures pause the run
A1–A3 have no failure edge to `X` or `H1`. A dispatch failure there sets a run-level `paused` status that requires a human to resume. It is not a graph state and never reaches `T-HALTED` except through `H1`.

### 6. Operation scopes are default-deny and live in `stage_requirements`
Each model operation lists `allowed_reads` from a declared artifact catalog and one write root. C1 and D1 run before `E0`, so they use `sealed_inputs_only`, not `implementation_sandbox`. E1 may read the pinned source tree and protected tests (read-only per `E0`); writes stay confined to its write root.

### 7. Credential denylist is a schema-enforced floor
The schema requires eight credential and billing-redirect environment variables to be listed; entries may be added but not removed. Child processes receive an allowlisted environment.

### 8. Provenance events are hash-chained
Every event carries `prev_hash` and `event_hash`. A new `model_result` event correlates with `model_call` through `operation_id` and `invocation_id`.

## Consequences

- Validators in `src/agentic_pipeline/` must cross-check `stage_requirements` against the graph, the artifact catalog, workspace roots, and isolation profiles (follow-up PR).
- All model stages stay blocked on hosts where auth, overage settings, or sandbox enforcement cannot be verified.
