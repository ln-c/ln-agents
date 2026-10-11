# Integration notes for the existing `agentic-pipeline` repository

This is an **overlay**, not a replacement repository. It is based on the supplied README and keeps the existing architecture intact.

## Copy into the repository root

```text
AGENTS.md                              -> ./AGENTS.md
CLAUDE.md                              -> ./CLAUDE.md
agents/*.md                            -> ./agents/*.md
policy/model_registry.yaml             -> ./policy/model_registry.yaml
schemas/model_registry.schema.json     -> ./schemas/model_registry.schema.json
```

Do not replace `pipeline/graph.yaml`, any existing `policy/*.yaml`, `src/agentic_pipeline/`, tests, or the current `README.md` with this overlay. Those existing repository components remain the source of truth. The new model registry refers to them by their documented paths.

## Follow-up integration tasks

1. Add `policy/model_registry.yaml` to the existing `CODEOWNERS` policy ownership rule; do not replace the current CODEOWNERS file with a snippet.
2. Add a test that parses the YAML and validates it against `schemas/model_registry.schema.json` (Draft-07), plus invariant checks that paid API billing stays disabled and proposal isolation stays enabled.
3. Extend `uv run python -m agentic_pipeline validate` to validate the registry if that CLI supports extension; otherwise add a dedicated validation subcommand without weakening existing checks.
4. Confirm the actual `pipeline/graph.yaml` stage IDs and existing policy key conventions. The README alone does not expose those file contents, so any mismatch must be reconciled against the actual files rather than guessed.
5. Confirm how Claude Pro and Google AI Pro are invoked in your runtime. The registry assumes provider-supported subscription-backed tools, not direct consumer-subscription API access. If the runtime cannot invoke a subscription workflow in a supported way, use a human-mediated handoff or pause; never fall back to paid API billing.
6. Verify that sandbox/write-root isolation is enforced by code and OS permissions, not only by the Markdown instructions.

## Stage-count note

The supplied README says “across 7 phases” but lists eight numbered phase groups. It separately describes seven agent roles. This overlay treats the system as **seven agent roles and the eight listed workflow groups**; verify the intended terminology before changing the canonical README.
