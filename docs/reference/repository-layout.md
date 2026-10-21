# Repository layout

```
├── pipeline/graph.yaml        # the state machine: states, guarded edges, side effects
├── policy/                    # human-owned policy (CODEOWNERS enforced)
│   ├── model_registry.yaml    # providers, the model catalog, bindings, per-stage operations, isolation
│   ├── model_test_profiles.yaml  # conformance checks by capability
│   ├── budgets.yaml           # retry budgets by route
│   ├── vetoes.yaml            # deterministic hard vetoes
│   ├── weights.yaml           # MCDA weights and thresholds
│   ├── fast_path.yaml         # fast-path criteria (allowlist currently empty)
│   ├── data_classes.yaml      # data classification and cloud routing
│   ├── escalation.yaml        # allowed escalation agents per data class
│   ├── routing.yaml           # exception class to handler route
│   ├── task_classes.yaml      # task classification
│   ├── advisories.yaml        # dependency advisories
│   ├── approvers.yaml         # who may sign which human decision
│   ├── ci.yaml                # E2 and F1 checks, toolchain, thresholds
│   ├── release.yaml           # merge branch rules and protected paths
│   └── triage.yaml            # A2J threshold for a valid critique finding
├── schemas/                   # JSON Schema (Draft-07) for artifacts and provenance events
├── agents/                    # role specifications, one per agent; each has a model (registry rule R20), a brief (R21), covers its nodes (R22), and is passed to its model (instructions, not a security boundary)
├── scripts/                   # MC/DC and mutation wrappers run by the CI checks
├── src/agentic_pipeline/      # runner, gates, adapters, sandbox, approvals, release
├── rust/                      # ln-agents: terminal UI, ACP agent and A2A client; MCP tool loop and sandbox launcher to come
└── tests/                     # invariant, policy, schema and runner tests
```

## Files at the root

Four files have similar names and different readers:

| File | Written for | Read by | Changed by |
|---|---|---|---|
| [`AGENTS.md`](../../AGENTS.md) | The people and coding agents that change this repository | Coding tools such as Claude Code and Codex, while they work here. `ln-agents` never reads it. | The maintainer (CODEOWNERS) |
| [`CLAUDE.md`](../../CLAUDE.md) | The same: it holds one line, an import of `AGENTS.md` | Claude Code | The maintainer (CODEOWNERS) |
| [`agents/*.md`](../../agents/) | The pipeline's own agents | `ln-agents`, which sends each to its model and freezes it per run ([Agent specifications](../concepts/agent-specifications.md)) | The maintainer (CODEOWNERS) |
| [`LN-AGENTS.md`](../../LN-AGENTS.md) | A project that runs `ln-agents` | `ln-agents`: the front matter names the coordinator, the body tells it how to write ([The coordinator](../concepts/coordinator.md)). Code never writes it. | Whoever runs the project; for this repository, its maintainer |

The models a run calls do not load `AGENTS.md` or `CLAUDE.md` as instructions. The Claude adapter runs `claude -p` with `--safe-mode`, the Pi adapter with `--no-context-files`, and agy in a sandbox that does not mount the project. A project's own `AGENTS.md` is only a file in the pinned source tree, treated as data.
