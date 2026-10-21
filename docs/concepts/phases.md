# Phases

The graph has eight groups of states:

1. **Grounding** (`START`, `G`): an immutable context manifest pinning the commit, contract versions and data classes.
2. **Design** (`A1`–`A4`): independent proposals from two providers, one cross-critique, a local judge of every finding (`A2J`), synthesis that answers every valid blocking finding, validation.
3. **Scoring** (`B1`–`B3`, `DR`): hard vetoes, MCDA scoring, authorization.
4. **Human authority** (`H1`, `DR`, `BENCH`, `CHILD`): design approval and its sealed record, benchmarking, child runs.
5. **Build** (`C1`–`E4`): decomposition, frozen contract tests, sandboxed implementation, escalation.
6. **Exception router** (`X`): reproduces, classifies and routes failures under budget limits.
7. **Release** (`F1`–`RB`): independent CI, signed merge approval, reconciliation, deployment, rollback.
8. **Learning** (`L1`–`HR`): provenance, merged-only learning, offline recalibration with human review.
