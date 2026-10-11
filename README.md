# agentic-pipeline

Specification, human-owned policy, artifact schemas, and structural invariant checks for the agentic software delivery pipeline.

## Overview

The pipeline coordinates multi-model design, deterministic scoring, verification, and human oversight across 7 phases:

1. **Grounding (`G1`–`G4`)**: Immutable context manifest pinning commit SHA, active ADRs, and artifact classifications.
2. **Design (`A1`–`A4`)**: Independent multi-model proposals (Gemini, Claude), cross-critique, synthesis, and validation.
3. **Scoring & Authorization (`B1`–`B3`)**: Hard deterministic vetoes, MCDA scoring over calibrated outcomes, and authorization gates.
4. **Human Authority (`H1`, `ADR`, `BENCH`, `CHILD`)**: Human decision checkpoints, ADR approval, benchmarking, and child run fork.
5. **Build (`C1`–`E4`)**: Module decomposition, frozen contract tests, ephemeral sandbox execution (Pi + Qwen3 8B), and escalation paths.
6. **Exception Router (`X`)**: Reproduces, classifies, and routes exceptions under strict budget limits without mutating protected artifacts.
7. **Release (`F1`–`RB`)**: Independent CI, hash-bound merge approval, reconciliation rebase, deployment authorization, and rollback.
8. **Learning (`L1`–`HR`)**: Provenance logging, index refresh, offline scorer recalibration via human review.

## Structure

```
├── pipeline/
│   └── graph.yaml             # Spec: State machine definition and guarded paths
├── policy/                    # Human-owned policies (CODEOWNERS enforced)
│   ├── budgets.yaml           # Retry budgets (run, design, regen, local, etc.)
│   ├── fast_path.yaml         # Fast-path bypass criteria
│   ├── vetoes.yaml            # Deterministic hard veto rules
│   ├── weights.yaml           # MCDA criteria weights and thresholds
│   ├── data_classes.yaml      # Data classification & cloud routing rules
│   ├── escalation.yaml        # Allowed escalation agents per data class
│   └── routing.yaml           # Exception classification to handler routes
├── schemas/                   # JSON schemas (Draft-07) for pipeline artifacts
├── src/agentic_pipeline/      # Graph validation, schema checking, stub interfaces
└── tests/                     # Invariant, policy, and schema tests
```

## Getting Started

Using [`uv`](https://docs.astral.sh/uv/):

```bash
# Sync dependencies
uv sync

# Run invariant checks and tests
uv run pytest

# Check pipeline graph validity via CLI
uv run python -m agentic_pipeline validate

# Lint and format
uv run ruff check .
uv run ruff format --check .
```
