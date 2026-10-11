# Agentic Pipeline integration overlay

This bundle fits the repository layout described in the supplied `agentic-pipeline` README.

- `AGENTS.md`: root-level agent index and repository invariants.
- `CLAUDE.md`: Claude Code behavior and execution boundaries.
- `agents/`: the seven role specifications.
- `policy/model_registry.yaml`: subscription/local execution assignments; policy remains human-owned.
- `schemas/model_registry.schema.json`: Draft-07 schema for registry validation.
- `INTEGRATION.md`: copy destinations and required follow-up integration tasks.

It deliberately does not replace the existing graph, policies, application source, tests, or root README. Apply the overlay to the actual repository and run the project's checks before enabling automated writes.
