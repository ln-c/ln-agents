# Integration status

The overlay that added `AGENTS.md`, `CLAUDE.md`, `agents/`, the model registry and its schema has been applied. This file records what each follow-up task from that overlay became, so an open item is not mistaken for a done one. [`README.md`](README.md) describes the pipeline as it stands.

| # | Follow-up task | Status | Where |
|---|---|---|---|
| 1 | Put `policy/model_registry.yaml` under CODEOWNERS | Done | `.github/CODEOWNERS` covers `/policy/` and `/schemas/` |
| 2 | Validate the registry against its schema, with invariant tests | Done | `src/agentic_pipeline/registry.py`, `tests/test_registry.py` (PR-1) |
| 3 | `validate` checks the registry | Done | `validate` runs graph, policy and registry checks |
| 4 | Reconcile stage IDs and policy keys with the real files | Done | The registry's `stage_requirements` must equal the graph's model states (rule R4) |
| 5 | Confirm how Claude Pro and Google AI Pro are invoked | Partly done | Adapters invoke the `claude` and `agy` CLIs; readiness checks the executable, never login or plan state. A call that fails on auth or quota pauses the run. Which Claude model is used is still the CLI default (open) |
| 6 | Enforce sandbox and write-root isolation in code | Done for model workers and CI checks | `src/agentic_pipeline/sandbox.py`; the real-isolation tests need bwrap and skip where it is unusable |

## Open

- ADR 0003 (model registry stage requirements) is still `Proposed`; accepting it is a human decision.
- Not built: `E0`, `E4`, `ADR`, `BENCH`, `CHILD`, `D2`, `RB`, `L3`, `HR`. A run blocks at the first of them it reaches. See the status table in the README.
- `L2` records the merged SHA and the accepted ADRs; it does not mark an ADR implemented or refresh an index.
- The learning agent's `telemetry_version: "1.1"` envelope is a specification; the code appends `learning` events to the provenance log instead.
- Number of phases: the graph has eight groups of states and the overlay's seven agent roles map onto them; the README now says eight.
