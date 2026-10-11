# 0001. Agentic Delivery Pipeline Architecture

Date: 2026-10-11
Status: Accepted
Approver: @ln-c

## Context

Automated and AI-driven software development systems require rigorous boundaries to prevent correlated errors, hallucinated contract drift, and uncontrolled retry loops. Early prototypes conflated model confidence with decision weights, lacked deterministic hard vetoes, and had incomplete feedback exception routing.

## Decision

We establish an 8-phase delivery pipeline encoded as a machine-readable state machine (`pipeline/graph.yaml`) with strictly enforced structural invariants:

1. **Grounding (`G1`–`G4`)**: Creates an immutable context manifest pinning the git SHA, accepted ADRs, and artifact classifications.
2. **Design (`A1`–`A4`)**: Independent multi-model proposals (Gemini, Claude) without cross-visibility, followed by mutual critique, synthesis, and schema/constraint validation.
3. **Scoring & Authorization (`B1`–`B3`)**: Deterministic hard vetoes before any scoring. MCDA with human-owned and versioned weights. Fast-path authorization bypasses design approval only, never code checks or CI.
4. **Human Authority (`H1`, `ADR`, `BENCH`, `CHILD`)**: Explicit human checkpoints for design approval, empirical sandbox benchmarking, or child run creation.
5. **Build (`C1`–`E4`)**: Module decomposition, frozen contract tests, read-only protected test suites, ephemeral sandbox execution (Pi + Qwen3 8B), and bounded escalation.
6. **Exception Router (`X`)**: Reproduces and classifies exceptions by cause (contract defect, missing test, topology conflict, infrastructure). Bounded by per-route retry budgets. Forbidden from mutating contracts, tests, ADRs, policies, weights, or budgets.
7. **Release (`F1`–`RB`)**: Independent CI with ADR fitness functions, SHA-bound merge approvals, reconciliation rebasing, and separate deployment authorization.
8. **Learning (`L1`–`HR`)**: Provenance append-log recorded before each external call, merged-only index refresh, and human-gated offline evaluation.

## Consequences

- Structural invariants are programmatically validated in CI (`agentic_pipeline.graph.validate`).
- Changes to policy, schemas, and pipeline topology require CODEOWNERS approval from human maintainers.
- Models cannot unilaterally approve architectural changes, clear vetoes, adjust decision weights, or escape sandbox boundaries.
