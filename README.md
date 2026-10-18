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
| Human design approval | `H1`, `DR` | Built. `H1` is a signed approval (see below); `DR` then seals the approved candidate as the design decision the build reads. `DR` reached from the fast path needs a scored winner and blocks, because none is ever invented. |
| BENCH, CHILD | `BENCH`, `CHILD` | **Not built.** A run blocks here. |
| Build | `C1`, `D1`, `E1` | Model dispatch built. |
| Build | `E0` | **Not built.** It has no handler, so a run blocks before `E1`. |
| Build gates | `E2`, `E3`, `X` | Built. `E4` (cloud escalation) is **not built**. |
| CI | `F1` | Built. See "CI checks". |
| Release | `R1`, `M1`, `RC`, `RM`, `MERGED` | Built. `R1` is a signed merge approval; `M1` makes a local merge branch only. |
| Deploy | `R2` | Built, but with nothing to deploy it can only decline (`T-MERGED-UNDEPLOYED`). `D2`, `RB` and `T-RELEASED`, `T-ROLLED-BACK` are **not built**. |
| Learning | `L1`, `L2` | `L1` is the per-run hash-chained provenance log. `L2` runs when the merge lands and appends a `learning` event; it edits nothing in the repository (see "Learning"). |
| Learning | `L3`, `HR` | **Not built.** |

## Getting started

Requires Python 3.13+ and [`uv`](https://docs.astral.sh/uv/); `ln-hub` in `rust/` needs `rustup` (it installs the pinned toolchain). The control plane (`validate`, `run init/step/status/approve/decide`, the gates and approvals) runs on Linux, macOS and Windows; CI tests it on all three. Running model stages and CI checks also needs `bubblewrap` (`bwrap`) with unprivileged user namespaces, so for now only on Linux (elsewhere readiness reports the sandbox UNVERIFIED and those stages block), plus `git` and `ssh-keygen` (OpenSSH 8.2+ for `-Y`). Local models run through a llama.cpp `llama-server` or an OpenVINO Model Server on loopback (see `providers` in `policy/model_registry.yaml`). Start `llama-server` with `-dev Vulkan0` and an `--alias` equal to the catalog `model_id`, for example `llama-server -m Qwen3.5-9B-Q4_K_M.gguf --alias qwen3.5-9b --host 127.0.0.1 --port 8080 -dev Vulkan0 -ngl 99 -c 16384 --jinja --parallel 1`: with `ggml-openvino` installed a bare `llama-server` picks the OpenVINO CPU backend. The decision model Clef-Flash runs in a second `llama-server` on port 8081 (`--alias clef-flash -b 4096 -ub 4096`, the full command is in the registry); it answers only on `/v1/systemone` and judges the critique findings at `A2J`, so start it before a design run reaches A2.

```bash
uv sync --all-groups

uv run pytest                                 # tests; the sandbox tests skip where bwrap is unusable
uv run python -m agentic_pipeline validate    # graph, policy and model-registry invariants
uv run ruff check . && uv run ruff format --check .

# ln-hub (rust/, toolchain pinned in rust/rust-toolchain.toml); CI runs it on Linux, Windows and macOS
cd rust && cargo fmt --check && cargo clippy --all-targets --locked -- -D warnings && cargo test --locked
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

A run that dispatches a model stage and fails (quota, auth, an adapter error) pauses; `resume` records a human decision to continue. `step` reports when it blocked and why. An operation that must reply with a JSON object (`output_schema` in the registry: `A1`, `A2`, `A3`, `A3R`) is checked as soon as it answers, the way the gates will read it: a reply that is not one object of its schema is logged as `invalid_output`, set aside under the attempt's `rejected/`, and that operation alone is asked again with the same inputs. A re-ask charges no budget; after two re-asks (`OUTPUT_RETRIES` in `dispatch.py`) the run pauses.

`serve a2a` exposes the same runs to other agents (such as `ln-hub`) as the A2A agent `ln-pipeline`. It needs the `a2a` extra (`uv sync --extra a2a`), binds a literal loopback address only, and gates everything but the agent card behind a bearer token (created with mode 0600 if missing).

```bash
uv run python -m agentic_pipeline serve a2a [--runs-dir D] [--host 127.0.0.1] [--port 8420] [--token-file F]
```

A request is one A2A message with one data part naming a skill:
- `start_run` (`run_id`, `entry_spec`);
- `advance` (`run_id`, or the task's run);
- `status`;
- `evidence`;
- `approval_document` (`decision`, `approver`, `reason`).

Each skill calls the same code as `run`. **None of them signs, decides or resumes:** the human still signs with `ssh-keygen` and runs `run decide` or `run resume`.

A request is checked before any task is touched. A refused request is a JSON-RPC error and leaves every task as it was. `status`, `evidence` and `approval_document` are answered with a message, even when the request names a task. A message that names a task without a `contextId` gets the task's context.

`advance` streams every provenance event as a WORKING status update, then ends in one of these states:
- `TASK_STATE_INPUT_REQUIRED` when the run awaits a human, with the evidence attached as an artifact;
- `TASK_STATE_AUTH_REQUIRED` when the run paused on `auth_failed` or `quota_exhausted`;
- `TASK_STATE_INPUT_REQUIRED` when the run is otherwise paused, blocked or out of steps (it is resumable);
- `TASK_STATE_COMPLETED` at a terminal state.

Because one A2A state covers several stops, the final status message also carries `{"run_status", "node"}`: `awaiting_human`, `paused`, `blocked`, `out_of_steps`, `stopped` or `finished`. A blocked step spent nothing. Advance retries it, but it blocks again until its cause is fixed. `ln-hub` says a run "needs a human" only for `awaiting_human`.

Tasks are stored under `<runs-dir>/.a2a/tasks` and answered from the run's own state after a restart.

`ln-hub acp` puts those skills in an editor that speaks ACP (Zed, JetBrains, Neovim plugins). The editor starts it and talks JSON-RPC over stdio. It calls the A2A server above, which must be running.

```bash
cd rust && cargo build --release      # target/release/ln-hub (about 1.7 MB; about 6 MB resident when idle)
ln-hub acp [--a2a-url http://127.0.0.1:8420/a2a/jsonrpc] [--token-file F]
```

The token file defaults to `<session cwd>/.runs/.a2a/token`, the server's default when the editor opens this repository. You can also set it, and the URL, through `LN_HUB_TOKEN_FILE` and `LN_HUB_A2A_URL`. The URL must be a literal loopback address, and on Unix a token file that others can read is refused. To use it from Zed, add it under `agent_servers` with the command `ln-hub` and the arguments `["acp"]`.

Every prompt is a slash command that runs as code; free text gets the help.
- `/runs`, `/status [run]` and `/evidence [run]` work in either mode.
- `/start <run> <entry_spec.json>`, `/advance [run]` and `/approval-doc <decision> <approver> <reason>` need the `design` mode. A session starts in the read-only `status` mode.
- `/approval-doc` asks the editor once (allow or reject, never "always") before writing the unsigned document. It then shows the `ssh-keygen` and `run decide` commands.
- `/approve`, `/decide` and `/resume` only explain that a human does these in a terminal.

While a run advances, the editor shows each state as a plan entry and each model call as a tool call. When the run stops, it shows the evidence. Cancelling ends the turn at once and cancels the task, and the run stops after its current step. The session list holds the runs driven over A2A (`run/<id>`), and loading one shows its status. MCP servers that the editor offers are ignored: pipeline tools are policy.

### Human decisions

A human state (`H1` design, `R1` merge, `R2` deploy) stops the run. The decision is a signed document, not a command-line flag:

1. `run approve` prints the evidence the human is deciding on and writes an approval document that binds the decision to that evidence, including the run's provenance tip, so an approval is single-use.
2. The human signs it: `ssh-keygen -Y sign -f ~/.ssh/id_ed25519 -n agentic-pipeline-approval <document>`.
3. `run decide` verifies the signature offline against `policy/approvers.yaml`, recomputes the evidence and takes the edge only if it still matches.

Only the keys listed in `policy/approvers.yaml` count. A model process has no way to produce an approval, but the approver's key is the user's own login key here: an unprotected key readable by a model process running as the same user could forge one, so protect it with a passphrase or use a dedicated or hardware key.

### CI checks

`policy/ci.yaml` lists the checks that run at `E2` (acceptance, MC/DC, optional contract, performance and dead-code checks) and `F1` (integration, mutation). Each runs offline in the bwrap sandbox on a copy of the pinned tree with the patch overlaid, with a toolchain built from that tree's `uv.lock`. A required check that is unset is `not_evaluated`, never passed.

- **MC/DC** uses `pymcdc`, which measures only `unittest.TestCase` tests. Pytest-style tests do not count, so `E2` cannot pass on this repository's own code until it has `unittest` tests; such a run routes `E2` → `X` → `H1`.
- **Mutation** uses `mutmut` with a minimum score of 80.
- `scripts/` and `policy/` are protected paths: an implementation patch may not change them.

### Release

`R1` approves the exact candidate tree. `M1` then creates a local branch `merge/<run>-<tree prefix>` with the merge commit; it never writes the target branch. A human merges that branch. `MERGED` waits for the merge to land, records the merged commit, and runs `L2`. If the target moved first, `RC` re-runs `E2` and `F1` on the new tip when the changes don't overlap, and stops for a human when they do or when a protected path changed.

### Learning

`L1` is a per-run JSONL file with a SHA-256 hash chain, appended before each model call and fsynced. `L2` appends one `learning` event per merged SHA holding the digest of `gates/L2/<merged_sha>.json`, which records the merged SHA and the patch. It does not refresh an index; none exists yet.

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
│   ├── release.yaml           # merge branch rules and protected paths
│   └── triage.yaml            # A2J threshold for a valid critique finding
├── schemas/                   # JSON Schema (Draft-07) for artifacts and provenance events
├── agents/                    # role specifications (instructions, not a security boundary)
├── scripts/                   # MC/DC and mutation wrappers run by the CI checks
├── src/agentic_pipeline/      # runner, gates, adapters, sandbox, approvals, release
├── rust/                      # ln-hub: ACP agent and A2A client; MCP tool loop and sandbox launcher to come
└── tests/                     # invariant, policy, schema and runner tests
```

## Phases

The graph has eight groups of states:

1. **Grounding** (`START`, `G`): an immutable context manifest pinning the commit, contract versions and data classes.
2. **Design** (`A1`–`A4`): independent proposals from two providers, one cross-critique, a local judge of every finding (`A2J`), synthesis that answers every valid blocking finding, validation.
3. **Scoring** (`B1`–`B3`, `DR`): hard vetoes, MCDA scoring, authorization.
4. **Human authority** (`H1`, `DR`, `BENCH`, `CHILD`): design approval and its sealed record, benchmarking, child runs.
5. **Build** (`C1`–`E4`): decomposition, frozen contract tests, sandboxed implementation, escalation.
6. **Exception router** (`X`): reproduces, classifies and routes failures under budget limits.
7. **Release** (`F1`–`RB`): independent CI, signed merge approval, reconciliation, deployment, rollback.
8. **Learning** (`L1`–`HR`): provenance, merged-only learning, offline recalibration with human review.
