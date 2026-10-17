# agentic-pipeline

A policy-driven pipeline for agentic software delivery. A deterministic runner walks the state machine in `pipeline/graph.yaml`; models propose, critique and implement inside a sandbox, and humans alone approve designs and merges. Nothing a model emits can clear a veto, spend budget, or take a transition.

This repository holds the state machine, human-owned policy, artifact schemas, the runner that executes them, and the tests that pin the invariants. Read [`AGENTS.md`](AGENTS.md) for the invariants and [`CLAUDE.md`](CLAUDE.md) for the rules an agent working in this repo follows.

## Status

Build status of every graph state, checked against `DEFAULT_HANDLERS` in `src/agentic_pipeline/runner.py` and `stage_requirements` in `policy/model_registry.yaml`. A state with no handler and no model operation **blocks the run**: the runner never guesses a guard.

| Phase | States | Status |
|---|---|---|
| Grounding | `START`, `G1`–`G4` | Built. Pins the commit, the context manifest and ADRs, and classifies data. |
| Design | `A1`–`A4`, `A3R` | Built. `A1`–`A3` and `A3R` dispatch to models; `A2` (blocking flaws) and `A4` (validation) gate on their output. |
| Scoring | `B1`–`B3`, `DR` | Built. Deterministic vetoes, MCDA scoring and the fast-path check. The fast path stays closed: its allowlist is empty, so every run goes to `H1`. |
| Human design approval | `H1` | Built. A signed approval (see below). |
| ADR, BENCH, CHILD | `ADR`, `BENCH`, `CHILD` | **Not built.** A run blocks here. |
| Build | `C1`, `D1`, `E1` | Model dispatch built. |
| Build | `E0` | **Not built.** It has no handler, so a run blocks before `E1`. |
| Build gates | `E2`, `E3`, `X` | Built. `E4` (cloud escalation) is **not built**. |
| CI | `F1` | Built. See "CI checks". |
| Release | `R1`, `M1`, `RC`, `RM`, `MERGED` | Built. `R1` is a signed merge approval; `M1` makes a local merge branch only. |
| Deploy | `R2` | Built, but with nothing to deploy it can only decline (`T-MERGED-UNDEPLOYED`). `D2`, `RB` and `T-RELEASED`, `T-ROLLED-BACK` are **not built**. |
| Learning | `L1`, `L2` | `L1` is the per-run hash-chained provenance log. `L2` runs when the merge lands and appends a `learning` event; it edits nothing in the repository (see "Learning"). |
| Learning | `L3`, `HR` | **Not built.** |

## Getting started

Requires Python 3.13+ and [`uv`](https://docs.astral.sh/uv/). Running model stages and CI checks also needs `bubblewrap` (`bwrap`) with unprivileged user namespaces, `git`, and `ssh-keygen`. Local models run through Ollama or an OpenVINO Model Server on loopback (see `providers` in `policy/model_registry.yaml`).

```bash
uv sync --all-groups

uv run pytest                                 # tests; the sandbox tests skip where bwrap is unusable
uv run python -m agentic_pipeline validate    # graph, policy and model-registry invariants
uv run ruff check . && uv run ruff format --check .
```

### Commands

`validate` checks the repository files and exits 1 on any violation. `preflight` is a read-only inspection of whether this host can run model stages; `--stage S` exits 1 unless stage `S` may be dispatched.

```bash
uv run python -m agentic_pipeline validate [--root R]
uv run python -m agentic_pipeline preflight [--root R] [--stage S]
uv run python -m agentic_pipeline models discover [--root R]
uv run python -m agentic_pipeline models test --model M [--root R]
```

`models discover` is read-only: it lists the model catalog (`models` in the registry). For each bound model it shows its provider and model id, family, location, capabilities, the operations it is bound to, its readiness and its conformance status (`ok`, `missing`, `stale`, `failed` or `unverifiable`). `models test` makes **live** model calls through that model's adapter, so it uses the provider's subscription allowance (not pipeline budget). It refuses unless the dispatcher would also allow a call, and it records `.conformance/M.json` (host evidence, gitignored). A record binds to the adapter's reported model identity and the policy it was run under, and goes stale when either changes. Dispatch does not yet require a current record, and the checks are smoke tests of the output contract, not a measure of quality.

`run` drives one pipeline run. Run state lives under `.runs/<run_id>` (`--runs-dir` changes it).

```bash
uv run python -m agentic_pipeline run init   <run_id> [--entry START] [--entry-spec FILE]
uv run python -m agentic_pipeline run step   <run_id> [--max-steps N]   # advance until blocked, paused or awaiting a human
uv run python -m agentic_pipeline run status <run_id>
uv run python -m agentic_pipeline run approve <run_id> --decision G --approver ID --reason TEXT \
    [--candidate C] [--remediation-cause code_defect|test_defect|design_issue] [--out FILE]
uv run python -m agentic_pipeline run decide  <run_id> --approval FILE [--signature FILE]
uv run python -m agentic_pipeline run resume  <run_id> --by WHO --reason TEXT   # interactive terminal only
```

A run that dispatches a model stage and fails (quota, auth, an adapter error) pauses; `resume` records a human decision to continue. `step` reports when it blocked and why.

### Human decisions

A human state (`H1` design, `R1` merge, `R2` deploy) stops the run. The decision is a signed document, not a command-line flag:

1. `run approve` prints the evidence the human is deciding on and writes an approval document that binds the decision to that evidence, including the run's provenance tip, so an approval is single-use.
2. The human signs it: `ssh-keygen -Y sign -f ~/.ssh/id_ed25519 -n agentic-pipeline-approval <document>`.
3. `run decide` verifies the signature offline against `policy/approvers.yaml`, recomputes the evidence and takes the edge only if it still matches.

Only the keys listed in `policy/approvers.yaml` count. A model process has no way to produce an approval, but the approver's key is the user's own login key here: an unprotected key readable by a model process running as the same user could forge one, so protect it with a passphrase or use a dedicated or hardware key.

### CI checks

`policy/ci.yaml` lists the checks that run at `E2` (acceptance, MC/DC, optional contract, performance and dead-code checks) and `F1` (integration, mutation, optional ADR fitness). Each runs offline in the bwrap sandbox on a copy of the pinned tree with the patch overlaid, with a toolchain built from that tree's `uv.lock`. A required check that is unset is `not_evaluated`, never passed.

- **MC/DC** uses `pymcdc`, which measures only `unittest.TestCase` tests. Pytest-style tests do not count, so `E2` cannot pass on this repository's own code until it has `unittest` tests; such a run routes `E2` → `X` → `H1`.
- **Mutation** uses `mutmut` with a minimum score of 80.
- `scripts/` and `policy/` are protected paths: an implementation patch may not change them.

### Release

`R1` approves the exact candidate tree. `M1` then creates a local branch `merge/<run>-<tree prefix>` with the merge commit; it never writes the target branch. A human merges that branch. `MERGED` waits for the merge to land, records the merged commit, and runs `L2`. If the target moved first, `RC` re-runs `E2` and `F1` on the new tip when the changes don't overlap, and stops for a human when they do or when a protected path changed.

### Learning

`L1` is a per-run JSONL file with a SHA-256 hash chain, appended before each model call and fsynced. `L2` appends one `learning` event per merged SHA holding the digest of `gates/L2/<merged_sha>.json`, which lists the accepted ADRs of the pinned manifest. It does not mark any ADR implemented and does not refresh an index: marking an ADR implemented is a human change to `docs/adr`, and no index exists yet.

The chain is tamper-evident, not tamper-proof; immutable storage is host configuration.

## Structure

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
│   └── release.yaml           # merge branch rules and protected paths
├── schemas/                   # JSON Schema (Draft-07) for artifacts and provenance events
├── agents/                    # role specifications (instructions, not a security boundary)
├── docs/adr/                  # architecture decision records (human-owned)
├── scripts/                   # MC/DC and mutation wrappers run by the CI checks
├── src/agentic_pipeline/      # runner, gates, adapters, sandbox, approvals, release
└── tests/                     # invariant, policy, schema and runner tests
```

## Phases

The graph has eight groups of states:

1. **Grounding** (`START`, `G1`–`G4`): an immutable context manifest pinning the commit, active ADRs and data classes.
2. **Design** (`A1`–`A4`): independent proposals from two providers, cross-critique, synthesis, validation.
3. **Scoring** (`B1`–`B3`, `DR`): hard vetoes, MCDA scoring, authorization.
4. **Human authority** (`H1`, `ADR`, `BENCH`, `CHILD`): design approval, ADR acceptance, benchmarking, child runs.
5. **Build** (`C1`–`E4`): decomposition, frozen contract tests, sandboxed implementation, escalation.
6. **Exception router** (`X`): reproduces, classifies and routes failures under budget limits.
7. **Release** (`F1`–`RB`): independent CI, signed merge approval, reconciliation, deployment, rollback.
8. **Learning** (`L1`–`HR`): provenance, merged-only learning, offline recalibration with human review.
