# agentic-pipeline

A policy-driven pipeline for agentic software delivery. A deterministic runner walks the state machine in `pipeline/graph.yaml`; models propose, critique and implement inside a sandbox, and humans alone approve designs and merges. Nothing a model emits can clear a veto, spend budget, or take a transition.

This repository holds the state machine, human-owned policy, artifact schemas, the runner that executes them, and the tests that pin the invariants. Read [`AGENTS.md`](AGENTS.md) for the invariants and [`CLAUDE.md`](CLAUDE.md) for the rules an agent working in this repo follows.

## Status

Build status of every graph state, checked against `DEFAULT_HANDLERS` in `src/agentic_pipeline/runner.py` and `stage_requirements` in `policy/model_registry.yaml`. A state with no handler and no model operation **blocks the run**: the runner never guesses a guard.

| Phase | States | Status |
|---|---|---|
| Grounding | `START`, `G` | Built. `G` is one state: it pins the commit, classifies data, indexes the source tree and seals the context manifest in a single stage manifest. |
| Design | `A1`–`A4`, `A2J`, `A3R` | Built. `A1`–`A3` and `A3R` dispatch to models; `A2` checks the critiques once (no repair loop), `A2J` has a local decision model judge every finding against a human-owned threshold, and `A4` validates, requiring the synthesis to answer every blocking finding judged valid. |
| Scoring | `B1`–`B3` | Built. Deterministic vetoes, MCDA scoring and the fast-path check. The fast path stays closed: its allowlist is empty, so every run goes to `H1`. |
| Human design approval | `H1`, `DR` | Built. `H1` is a signed approval (see [Human decisions](docs/guides/human-decisions.md)); `DR` then seals the approved candidate as the design decision the build reads. `DR` reached from the fast path needs a scored winner and blocks, because none is ever invented. |
| BENCH, CHILD | `BENCH`, `CHILD` | **Not built.** A run blocks here. |
| Build | `C1`, `D1`, `E1` | Model dispatch built. |
| Build | `E0` | **Not built.** It has no handler, so a run blocks before `E1`. |
| Build gates | `E2`, `E3`, `X` | Built. `E4` (cloud escalation) is **not built**. |
| CI | `F1` | Built. See [CI checks](docs/reference/ci-checks.md). |
| Release | `R1`, `M1`, `RC`, `RM`, `MERGED` | Built. `R1` is a signed merge approval; `M1` makes a local merge branch only. |
| Deploy | `R2` | Built, but with nothing to deploy it can only decline (`T-MERGED-UNDEPLOYED`). `D2`, `RB` and `T-RELEASED`, `T-ROLLED-BACK` are **not built**. |
| Learning | `L1`, `L2` | `L1` is the per-run hash-chained provenance log. `L2` runs when the merge lands and appends a `learning` event; it edits nothing in the repository (see [Release and learning](docs/concepts/release-and-learning.md#learning)). |
| Learning | `L3`, `HR` | **Not built.** |

## Getting started

Requirements, the checks to run before a change, and the first-run walkthrough are in [Getting started](docs/getting-started.md). In an editor that speaks ACP, `/guideline` prints the same walkthrough for your repository.

## Documentation

The rest is in [`docs/`](docs/README.md): guides for tasks, concepts that explain how the pipeline works, and reference for commands and files.
