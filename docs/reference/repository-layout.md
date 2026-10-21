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
├── rust/                      # ln-agents: ACP agent and A2A client; MCP tool loop and sandbox launcher to come
└── tests/                     # invariant, policy, schema and runner tests
```
