# 0002. Open Decisions and Proposed Extensions

Date: 2026-10-11
Status: Proposed

## Context

While the pipeline skeleton and state machine invariants are formalized in ADR 0001, several parameters and policies require operational calibration and human review.

## Open Decisions

### 1. Scorer Calibration for Jev and Cloudflare Clef
In Phase B2, models provide calibrated outcome probabilities (e.g. scalability, security).
- **Question**: What labeled dataset/ground truth were these models calibrated against, and does that calibration transfer to our system domain?
- **Current Position**: In the absence of validated calibration metrics (ECE, reliability diagrams), Phase B2 outcome probabilities should be treated as ranking hints rather than definitive probabilities.

### 2. Fine-grained Budget Values per Task Class
Budgets currently initialize at `max_retries: 2` (3 attempts total) across all routes (`run`, `design`, `regen`, `evidence`, `local`, `escalation`, `test`, `infra`, `reconcile`, `remediation`, `runner_up`).
- **Question**: How should budgets scale by task class (e.g. trivial refactor vs. cross-subsystem migration)?
- **Current Position**: Start uniformly at 2 retries; adjust per task class after measuring Phase E execution cost and latency.

### 3. Hard Vetoes vs. Scored Criteria
In Phase B1, hard vetoes deterministically disqualify candidates prior to MCDA scoring.
- **Question**: Which checks should remain non-negotiable hard vetoes, and which should be scored trade-offs?
- **Current Position**: Schema invalidity, accepted ADR violation, data class violations, and frozen contract breaches remain hard vetoes.

### 4. Fast-path Exclusions
Phase B3 allows pre-implementation fast-path authorization for low-risk changes.
- **Proposed Exclusions**:
  - `near_tie`
  - `high_uncertainty`
  - `security_tag`
  - `new_dependency` (proposed)
  - `cross_service_boundary` (proposed)
- **Question**: Should `new_dependency` and `cross_service_boundary` always require human approval?

### 5. Child Run Fork Limit
When requirements or constraints change, a child run is authorized (`CHILD` -> `G1`).
- **Question**: Should child runs be strictly capped at 1 child per root task to prevent infinite derivation trees?

### 6. Loop Budget on Runner-up Reuse
When a topology conflict occurs in Phase E, the exception router may route to `A4` using the retained runner-up candidate.
- **Decision**: A dedicated `runner_up` budget key was added to prevent indefinite cycling between `X` and `A4`.
