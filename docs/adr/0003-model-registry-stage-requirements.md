# 0003. Model Registry Stage Requirements and Model Binding

Date: 2026-10-12
Status: Proposed

## Context

The model registry overlay (`policy/model_registry.yaml`) and `pipeline/graph.yaml` disagreed on several points, and the registry had no per-stage operation scopes for the runtime to enforce. Each item below is a human-owned decision; nothing here takes effect until @ln-c approves it.

Concrete models change faster than this architecture does. The decisions below therefore fix **roles, constraints and where a model is named**, not which model fills a role. Wherever a decision names a provider, that is the *current binding* recorded in the registry at the time of writing; it is not part of the decision, and changing it needs no new ADR as long as the constraints in decisions 9–11 still hold.

## Decisions

### 1. Design A and Design B are two independent model families
Designs A and B must come from different model families, in separate sessions (decision 10 defines independence). Current binding: A = `claude_pro`, B = `google_ai_pro`. `AGENTS.md` (invariant 4) and `CLAUDE.md` still state this binding as a rule; this ADR does not change them, and aligning them with this decision is a follow-up human edit. Earlier, only the free-text `doc:` on graph state `A1` said the reverse. That string now names no model, so the registry is its only source; no state or edge changes.

### 2. Build runs on a local model; cloud models reach build only through escalation
The build stages (`C1`, `D1`, `E1`) use a local model, and cloud models reach build only through the graph's `E3 → E4` escalation path, which is disabled in `stage_requirements` until `policy/escalation.yaml` targets map to registry providers. Current binding: `qwen_local`. The registry previously named `claude_pro` primary with Qwen as fallback.

### 3. A3 synthesis keeps `deterministic_orchestrator` as owner
The orchestrator owns synthesis; one model from `model_assistance`, in the order the registry lists, drafts it. Current binding: Claude first, then Google. `A4` validates the result deterministically.

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

### 9. Models are bound in the registry and nowhere else
A role (agent, stage operation) is stable; the model that fills it is a registry entry. The registry names concrete models, and the ADRs, the graph, agent specifications and tests do not (the graph's `doc:` strings are descriptive only). The binding point is the agent's entry in `agents` (`primary`, then `model_assistance` in preference order). `stage_requirements` names no provider: an operation's candidate backends are derived from its agent (`all_of` uses the `primary`; `ordered_any_of` tries the `primary` when it is a model provider, then each `model_assistance` entry), and the schema rejects a `backends` list there. Changing a binding is a registry change reviewed through CODEOWNERS; it must still pass `validate`, which re-checks decisions 10 and 11 and the capability rule below. Introducing a new provider or runtime still needs an adapter and its tests once; re-binding an existing provider does not.

A provider declares the `capabilities` it claims (`classification`, `code_generation`, `design_reasoning`) and each operation states what it `requires`; rule R18 fails when any candidate backend lacks one, so rebinding the build agent to the classifier fails validation. Capabilities are declared, not measured: nothing yet checks that a model has what it claims, and the recorded conformance results planned in the follow-ups are what would. A provider is the unit that is bound, because adapters, readiness, dispatch and provenance all key on the provider id. A separate catalog of models, so that one provider could serve several at once, is deferred until a provider needs to.

### 10. Independence is judged on model family, not provider id
A provider is an access path; a model family (`claude`, `gemini`, `qwen3`, ...) is what must differ. Each model provider declares a `family`, or lists ordered `family_by_model_id` patterns that are matched against its pinned model id, so a provider that can serve several families (`agy` also offers Claude and GPT-OSS models) cannot be re-pointed at a family already in use without failing validation. Rule R14 requires distinct families for operations under session separation, and the A2 and A3R gates apply the same comparison to the recorded backends at run time. A family that cannot be resolved is an error (R17) and never counts as independent. Limit: some providers report no resolved model (the `agy` adapter records the requested model), so this check covers the model the registry requests, not the model the provider confirms it served.

### 11. Data location is declared and fails closed
Each provider declares `location: local | cloud` (rule R16 checks it against its access mode, and an unknown access mode is an error). Dispatch applies `policy/data_classes.yaml` to every provider whose location is not `local`, so a missing or unrecognised value is treated as cloud. Previously this was a set of access-mode names in code, and a mode outside that set was treated as local.

## Consequences

- Validators in `src/agentic_pipeline/` cross-check `stage_requirements` against the graph, the artifact catalog, workspace roots, isolation profiles, provider locations and model families (`registry.py`, R1–R18).
- Not yet done, in later changes: a model catalog separate from providers (when one provider must serve several models at once), per-model context limits (nothing consumes them yet), and recorded conformance results for each bound model. A setup command that proposes bindings would emit a diff for human review; it would not write `policy/`.
- The `google_ai_pro` family patterns (`^gemini-`, `^claude-`, `^gpt-oss`) were checked against the output of `agy models` (13 ids: Gemini 4 Argon and 3.8 Flash, Claude Opus 5.5 and Sonnet 5.5, GPT-OSS 120B), and every id resolves to its family. A model id that matches no pattern, including one from a family agy adds later, fails validation, so a stale list blocks rather than weakens independence.
- All model stages stay blocked on hosts where a provider's agent is unavailable (executable missing or below the minimum version), a prohibited credential variable is set, or sandbox enforcement cannot be verified. The pipeline does not inspect login state, plan, or billing settings; those are the user's responsibility, and a call that fails on auth or quota is reported by the adapter at run time.
