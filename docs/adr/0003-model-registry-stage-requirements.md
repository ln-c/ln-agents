# 0003. Model Registry Stage Requirements and Model Binding

Date: 2026-10-12
Status: Proposed

## Context

The model registry overlay (`policy/model_registry.yaml`) and `pipeline/graph.yaml` disagreed on several points, and the registry had no per-stage operation scopes for the runtime to enforce. Each item below is a human-owned decision; nothing here takes effect until @ln-c approves it.

Concrete models change faster than this architecture does. The decisions below therefore fix **roles, constraints and where a model is named**, not which model fills a role. Wherever a decision names a provider, that is the *current binding* recorded in the registry at the time of writing; it is not part of the decision, and changing it needs no new ADR as long as the constraints in decisions 9–11 still hold.

## Decisions

### 1. Design A and Design B are two independent model families
Designs A and B must come from different model families, in separate sessions (decision 10 defines independence). Current binding: A = `claude_cli_default` (Claude Pro), B = `gemini_3_8_flash_high` (Google AI Pro via `agy`). `AGENTS.md` (invariant 4) and `CLAUDE.md` still state this binding as a rule; this ADR does not change them, and aligning them with this decision is a follow-up human edit. Earlier, only the free-text `doc:` on graph state `A1` said the reverse. That string now names no model, so the registry is its only source; no state or edge changes.

### 2. Build runs on a local model; cloud models reach build only through escalation
The build stages (`C1`, `D1`, `E1`) use a local model, and cloud models reach build only through the graph's `E3 → E4` escalation path, which is disabled in `stage_requirements` until `policy/escalation.yaml` targets map to registry providers. Current binding: `qwen3_8b` (local, through Ollama). The registry previously named `claude_pro` primary with Qwen as fallback.

### 3. A3 synthesis keeps `deterministic_orchestrator` as owner
The orchestrator owns synthesis; one model from `model_assistance`, in the order the registry lists, drafts it. Current binding: `claude_cli_default` first, then `gemini_3_8_flash_high`. `A4` validates the result deterministically.

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

### 9. A model catalog; roles bind to models, in the registry and nowhere else
A role (agent, stage operation) is stable; the model that fills it is a registry entry. `providers` describe how the pipeline reaches a model (access mode, location, readiness); `models` is the catalog of what a provider can serve, one entry per model: `provider`, `model_id`, `family` and `capabilities`. A provider can serve several models at once (`agy` offers 13), so two roles may use different models of one provider at the same time. The registry names concrete models, and the ADRs, the graph, agent specifications and tests do not (the graph's `doc:` strings are descriptive only).

The binding point is the agent's entry in `agents` (`primary`, then `model_assistance` in preference order), and it names a catalog model (or a non-model provider such as `deterministic_rules`); naming a model provider there is an error (R3). `stage_requirements` names no model: an operation's candidates are derived from its agent (`all_of` uses the `primary`; `ordered_any_of` tries the `primary` when it is a model, then each `model_assistance` entry), and the schema rejects a `backends` list there. The model id the pipeline passes to a provider exists only in its catalog entry. Changing a binding is a registry change reviewed through CODEOWNERS; it must still pass `validate`, which re-checks decisions 10 and 11 and the capability rule below. Adding a provider or runtime still needs an adapter and its tests once; binding another catalog model does not.

A catalog entry declares the `capabilities` claimed for it (`classification`, `code_generation`, `design_reasoning`) and each operation states what it `requires`; rule R18 fails when any candidate model lacks one. An empty list is a known model nobody has claimed anything for, so it cannot be bound to any operation. Capabilities are declared, not measured: decision 12 is what checks them.

### 10. Independence is judged on model family, not provider id
A provider is an access path; a model family (`claude`, `gemini`, `qwen3`, ...) is what must differ, and each catalog entry declares its `family`. Every model provider lists ordered `family_patterns` over model ids (a provider may serve several families: `agy` also offers Claude and GPT-OSS models, and Ollama serves whatever is pulled), and every one of its models must declare the family its id matches (rule R17), so a Claude model cannot be catalogued as Gemini to pass as independent, and an id no pattern recognises blocks until a human adds one. Rule R14 requires distinct families for operations under session separation, and the A2 and A3R gates apply the same comparison to the recorded backends at run time. A backend with no catalog entry has no family and never counts as independent. Limit: some providers report no resolved model (the `agy` adapter records the requested model), so this check covers the model the registry requests, not the model the provider confirms it served.

### 11. Data location is declared and fails closed
Each provider declares `location: local | cloud` (rule R16 checks it against its access mode, and an unknown access mode is an error). Dispatch applies `policy/data_classes.yaml` to every provider whose location is not `local`, so a missing or unrecognised value is treated as cloud. Previously this was a set of access-mode names in code, and a mode outside that set was treated as local.

### 12. Conformance is recorded evidence about a model, derived from its roles
`policy/model_test_profiles.yaml` maps each capability to smoke checks (`identity`, `json_reply`, `file_output`). A model's required checks are the union over the `requires` of every operation it can be bound to, so rebinding changes them. `models test --model M` runs them through the model's real adapter and writes `.conformance/M.json`; the call spends the provider's subscription allowance, so it runs only when a human invokes it, and it first applies the dispatcher's own pre-call gates (credential denylist, readiness, isolation). A record binds to the adapter's reported identity (`model`, `model_version`, `access_method`), the model's and its provider's registry entries, the profiles file and the required-check list, and any change makes it stale; a missing, stale or failed record is never a pass. Limits: checks show an adapter path and output contract work, not that a model is good at its role; records are unsigned and tamper-evident only through their digest; a provider that cannot report the model that answered (agy) is identified by the model the registry requested. Dispatch does not yet require a current record; that gate, with the record digest pinned in `model_call`, is a later change.

## Consequences

- Validators in `src/agentic_pipeline/` cross-check `stage_requirements` against the graph, the artifact catalog, workspace roots, isolation profiles, provider locations and model families (`registry.py`, R1–R18).
- Not yet done, in later changes: per-model context limits (nothing consumes them yet), a dispatch gate that requires a current conformance record (decision 12 records them but does not enforce them), and the setup workflow that lists what each provider offers now, suggests bindings for the catalog, and proposes a registry diff for human review (it would not write `policy/`).
- The `google_ai_pro` family patterns (`^gemini-`, `^claude-`, `^gpt-oss`) were checked against the output of `agy models` (13 ids: Gemini 4 Argon and 3.8 Flash, Claude Opus 5.5 and Sonnet 5.5, GPT-OSS 120B), and every id resolves to its family. A catalog entry whose id matches no pattern, including one from a family agy adds later, fails validation, so a stale list blocks rather than weakens independence; agy offering a new model changes nothing until someone adds it to the catalog and binds it.
- All model stages stay blocked on hosts where a provider's agent is unavailable (executable missing or below the minimum version), a prohibited credential variable is set, or sandbox enforcement cannot be verified. The pipeline does not inspect login state, plan, or billing settings; those are the user's responsibility, and a call that fails on auth or quota is reported by the adapter at run time.
