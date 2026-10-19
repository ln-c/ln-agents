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

Requires Python 3.13+ and [`uv`](https://docs.astral.sh/uv/); `ln-agents` in `rust/` needs `rustup` (it installs the pinned toolchain). The control plane (`validate`, `run init/step/status/approve/decide`, the gates and approvals) runs on Linux, macOS and Windows; CI tests it on all three. Running model stages and CI checks also needs `bubblewrap` (`bwrap`) with unprivileged user namespaces, so for now only on Linux (elsewhere readiness reports the sandbox UNVERIFIED and those stages block), plus `git` and `ssh-keygen` (OpenSSH 8.2+ for `-Y`). Local models run through a llama.cpp `llama-server` or an OpenVINO Model Server on loopback (see `providers` in `policy/model_registry.yaml`). You do not start the llama.cpp servers by hand: each local provider's `launch` in the registry is how dispatch starts it when a stage needs it and it is not running, and it is stopped again after the stage (a server you started yourself is used and left running). Logs go to `<runs-dir>/.servers/<provider>.log`; a server that does not come up in time refuses the stage before anything is spent, and `preflight` shows a stopped one as "started on demand". `launch` must bind the loopback host and port the readiness probe checks (registry rule R19) and pin `-dev Vulkan0` (with `ggml-openvino` installed a bare `llama-server` picks the OpenVINO CPU backend). The decision model Clef-Flash runs on port 8081 with a 8192-token batch, because it answers only on `/v1/systemone` and judges each critique finding at `A2J` in one batch.

```bash
uv sync --all-groups

uv run pytest                                 # tests; the sandbox tests skip where bwrap is unusable
uv run python -m agentic_pipeline validate    # graph, policy and model-registry invariants
uv run ruff check . && uv run ruff format --check .

# ln-agents (rust/, toolchain pinned in rust/rust-toolchain.toml); CI runs it on Linux, Windows and macOS
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
uv run python -m agentic_pipeline run evidence <run_id>                  # write the readable report of a run awaiting a human
uv run python -m agentic_pipeline run views <run_id>                     # write the coordinator's Markdown views and the summary of where the run stands
uv run python -m agentic_pipeline run approve <run_id> --decision G --approver ID --reason TEXT \
    [--candidate C] [--remediation-cause code_defect|test_defect|design_issue] [--out FILE]
uv run python -m agentic_pipeline run decide  <run_id> --approval FILE [--signature FILE]
uv run python -m agentic_pipeline run resume  <run_id> --by WHO --reason TEXT   # interactive terminal only
```

`run init` copies the graph and every policy file into `.runs/<run_id>/frozen/` and records their digests. From then on the run reads only that copy, so editing `policy/` (a new model in the registry, a budget) affects the runs you start after the edit and leaves the ones in progress alone. A frozen copy that no longer matches its digests is refused, so the run will not reopen; a run created before this change has no copy and is still checked against `policy/`. Approvals are verified against the run's own `approvers.yaml`, so a key added to or removed from `policy/approvers.yaml` applies to new runs only.

A run that dispatches a model stage and fails (quota, auth, an adapter error) pauses; `resume` records a human decision to continue. `step` reports when it blocked and why. An operation that must reply with a JSON object (`output_schema` in the registry: `A1`, `A2`, `A3`, `A3R`) is checked as soon as it answers, the way the gates will read it: a reply that is not one object of its schema is logged as `invalid_output`, set aside under the attempt's `rejected/`, and that operation alone is asked again with the same inputs. A re-ask charges no budget; after two re-asks (`OUTPUT_RETRIES` in `dispatch.py`) the run pauses.

`serve a2a` exposes the same runs to other agents (such as `ln-agents`) as the A2A agent `ln-pipeline`. It needs the `a2a` extra (`uv sync --extra a2a`), binds a literal loopback address only, and gates everything but the agent card behind a bearer token (created with mode 0600 if missing).

```bash
uv run python -m agentic_pipeline serve a2a [--runs-dir D] [--host 127.0.0.1] [--port 8420] [--token-file F]
```

A request is one A2A message with one data part naming a skill:
- `start_run` (`run_id`, `entry_spec`);
- `advance` (`run_id`, or the task's run);
- `status`;
- `evidence`: the evidence, the choices a human can sign, who may sign, and the readable report (its Markdown and the path it was written to);
- `approval_document` (`decision`, `approver`, `reason`, and `candidate` for a design approval);
- `ask` (`question`, and `run_id` unless the task has one): the coordinator's text answer, from the run's Markdown views (see [The coordinator](#the-coordinator)).
- `models` (no arguments, no run): the model catalog of the repository's registry, the rows `models discover` prints: provider, family, location, capabilities, what each model is bound to and, when bound, its readiness and conformance status. It is read-only and calls no model. If the repository fails `validate`, the reply carries the errors instead of a catalog.
- `agents` (no arguments, no run): which model each agent slot of the registry is bound to and what runs on it, and the registry changes waiting for a human.
- `registry_change` (`kind` and its fields, no run): draft one change to `policy/model_registry.yaml`. `add_model` (`provider`, `model_id`, `capabilities`) adds a model, or claims capabilities for one the catalog already has; `remove_model` (`model`) removes one nothing is bound to; `bind` (`slot`, `models`) binds an agent slot. Code checks the result against every registry rule and stores it with a diff. It writes nothing to `policy/`.
- `propose_mapping` (`intent`, optional; no run): ask the authority model to propose bindings for the agent slots. The model is the one `LN-AGENTS.md` names, and only if the registry binds it to `human_authority`. Code checks every binding and stores what survives as a draft, like `registry_change`; it writes nothing to `policy/`. It calls the model, so it uses its subscription allowance.
- `apply_change` (`change_id`): write a stored draft, if the registry is still the file it was made from and still passes every rule. It takes an id and no text.
- `discard_change` (`change_id`): drop a stored draft.

Each skill calls the same code as `run`. **None of them signs, decides or resumes:** the human still signs with `ssh-keygen` and runs `run decide` or `run resume` (`ln-agents` opens a terminal for that, see below). The one policy file they can change is `policy/model_registry.yaml`, and only through `apply_change`: see [Choosing models](#choosing-models). Budgets, vetoes, approvers and the graph cannot be reached.

A request is checked before any task is touched. A refused request is a JSON-RPC error and leaves every task as it was. `status`, `evidence`, `ask`, `approval_document` and `models` are answered with a message, even when the request names a task. A message that names a task without a `contextId` gets the task's context.

`advance` streams every provenance event as a WORKING status update, then ends in one of these states:
- `TASK_STATE_INPUT_REQUIRED` when the run awaits a human, with the evidence attached as an artifact;
- `TASK_STATE_AUTH_REQUIRED` when the run paused on `auth_failed` or `quota_exhausted`;
- `TASK_STATE_INPUT_REQUIRED` when the run is otherwise paused, blocked or out of steps (it is resumable);
- `TASK_STATE_COMPLETED` at a terminal state.

Because one A2A state covers several stops, the final status message also carries `{"run_status", "node"}`: `awaiting_human`, `paused`, `blocked`, `out_of_steps`, `stopped` or `finished`. A blocked step spent nothing. Advance retries it, but it blocks again until its cause is fixed. `ln-agents` says a run "needs a human" only for `awaiting_human`.

Tasks are stored under `<runs-dir>/.a2a/tasks` and answered from the run's own state after a restart.

`ln-agents acp` puts those skills in an editor that speaks ACP (Zed, JetBrains, Neovim plugins). The editor starts it and talks JSON-RPC over stdio. It calls the A2A server above, which must be running.

```bash
cd rust && cargo build --release      # target/release/ln-agents (about 1.7 MB; about 6 MB resident when idle)
ln-agents acp [--a2a-url http://127.0.0.1:8420/a2a/jsonrpc] [--token-file F]
```

The token file defaults to `<session cwd>/.runs/.a2a/token`, the server's default when the editor opens this repository. You can also set it, and the URL, through `LN_AGENTS_TOKEN_FILE` and `LN_AGENTS_A2A_URL`. The URL must be a literal loopback address, and on Unix a token file that others can read is refused. To use it from Zed, add it under `agent_servers` with the command `ln-agents` and the arguments `["acp"]`.

Every prompt is a slash command that runs as code; free text gets the help.
- `/runs`, `/status [run]`, `/evidence [run]`, `/models` and `/agents` work in either mode. `/models` lists the registry's models: a table of those bound to an operation (provider and id, family, where it runs, capabilities, what it is bound to, readiness, conformance), then the others by name, split into those that declare capabilities and can be bound and those with none claimed.
- Anything you type that is not a slash command is a question for the coordinator model (see [The coordinator](#the-coordinator)), asked about the session's run. The answer is text, from the run's Markdown views; it starts, advances, signs and resumes nothing, and `/help` or `help` shows the commands.
- `/start <run> <entry_spec.json>`, `/advance [run]` and `/approval-doc <decision>[:<candidate>] <approver> <reason>` need the `design` mode. A design approval names its candidate, e.g. `approve_design_and_no_security_veto:synthesis_sealed`. A session starts in the read-only `status` mode.
- `/approval-doc` asks the editor once (allow or reject, never "always") before writing the unsigned document. It then shows the `ssh-keygen` and `run decide` commands you could run by hand, and remembers the document for `/decide`.
- `/decide` (design mode) opens a terminal window for the document `/approval-doc` wrote in this session. ln-agents does not sign or decide: the window runs `ln-agents decide`, which shows the run, node, decision, candidate, approver and reason, asks you to type the run id, runs `ssh-keygen -Y sign` so it asks for your passphrase there, and then runs `run decide`, which verifies the signature against `policy/approvers.yaml` as before. The window drops `SSH_AUTH_SOCK` and `SSH_ASKPASS_REQUIRE`, so a key loaded in an ssh-agent still asks for its passphrase. A key without a passphrase is protected only by the typed run id. An earlier `.sig` next to the document is set aside while signing and put back if the signing fails.
- `/resume <who> <reason>` (design mode) opens a terminal that runs `run resume`, which shows why the run paused and asks you to type the run id. A resume is still self-asserted (`--by`); it has no passphrase.
- `/add_model <provider> <model_id> [capability ...]`, `/remove_model <model>`, `/agents <slot> <model>[,<model> ...]`, `/agents auto [what you want]`, `/confirm <id>` and `/discard <id>` (design mode) draft and confirm changes to the model registry, see [Choosing models](#choosing-models).
- `/approve`, `/sign` and `/reject` are not commands.

The window is opened with `LN_AGENTS_TERMINAL` (the words that open one, such as `kitty` or `alacritty -e`), else `$TERMINAL`, `xdg-terminal-exec` or the first of foot, kitty, alacritty, wezterm, ghostty, gnome-terminal, konsole, xfce4-terminal and xterm on `PATH`. It runs the orchestrator as `LN_AGENTS_PIPELINE` (default `uv run python -m agentic_pipeline`), signs with `LN_AGENTS_SSH_KEY` (default `~/.ssh/id_ed25519_ln_approval`, the approval key), and `/resume` looks for runs in `LN_AGENTS_RUNS_DIR` (default `<session cwd>/.runs`). macOS and Windows have no known emulator yet, so set `LN_AGENTS_TERMINAL` there. If no window can be opened, ln-agents prints the commands to run by hand.

While a run advances, the editor shows each state as a plan entry and each model call as a tool call. When the run stops for a human, and on `/evidence`, it shows the choices and the candidates at a glance, with a link that opens the full report. When a run stops, it also shows the coordinator's summary of the stop, with a link that opens the file. Cancelling ends the turn at once and cancels the task, and the run stops after its current step. The session list holds the runs driven over A2A (`run/<id>`), and loading one shows its status. MCP servers that the editor offers are ignored: pipeline tools are policy.

### Human decisions

A human state (`H1` design, `R1` merge, `R2` deploy) stops the run. The decision is a signed document, not a command-line flag:

1. `run evidence` (or `run approve`, or `/evidence` in the editor) writes a readable report to `.runs/<run>/approvals/<node>-evidence.md`. It lists every choice, each candidate's components, data flows, dependencies, vetoes and scores, and every finding with its judged probability and the synthesis's answer. It ends with the digests a signature binds to. The report is a view, rebuilt from sealed state each time, and model text in it is escaped so it cannot turn into links or HTML. `run approve` writes an approval document that binds the decision to that evidence, including the run's provenance tip, so an approval is single-use.
2. The human signs it with the approval key: `ssh-keygen -Y sign -f ~/.ssh/id_ed25519_ln_approval -n agentic-pipeline-approval <document>`. (`/decide` in the editor does 2 and 3 in a terminal window.)
3. `run decide` verifies the signature offline against `policy/approvers.yaml`, recomputes the evidence and takes the edge only if it still matches.

Only the keys listed in `policy/approvers.yaml` count. A model process has no way to produce an approval, but a key it could read and use could forge one. So the approver's key is a dedicated one (`~/.ssh/id_ed25519_ln_approval`): it is not the login key or the git-signing key, it has a passphrase, and it is never loaded into an ssh-agent. A hardware key (`ssh-keygen -t ed25519-sk`) is stronger still.

### The coordinator

Every sealed document and gate record is JSON, which the gates read and an approval binds to. A model, the **coordinator**, writes the readable version: a Markdown view of each document, a summary of every stop, and answers to questions. It is named in the project's `LN-AGENTS.md`:

```markdown
---
coordinator: gemini_3_8_flash_high   # a model that declares `coordination` in the registry
fallback: some_other_model           # optional
---
Your instructions on tone and layout, in plain Markdown.
```

- **What it writes**, under `.runs/<run>/views/`: `A1-proposal_a.md`, `A2-critique_a_by_b.md` and so on (every sealed document from the task specification to the review of the synthesis, plus the gate records), and `STOP-<n>-<node>.md` for each stop: blocked, paused, waiting for a signed decision, or finished. At a human decision the summary is a briefing on the candidates, with a recommendation labelled as advice. `run views <run>` writes them from a terminal; the A2A server writes the summary when a run stops and the documents in the background.
- **What it cannot do:** start, advance, sign, resume or decide anything. The commands in a summary come from a fixed table in code; the model's text is never run or parsed into an action. The JSON stays the record: every view says so, and says which model wrote it.
- **What checks it:** the same readiness, credential and data-class checks as a stage (a class that policy keeps off the cloud is refused before any call), and the subscription tool's sandbox. A reply that leaves out an identifier of its record is replaced by the raw record. With no coordinator, or when it fails, every file is still written, from code.
- **Where it is logged:** in its own hash chain, `views/provenance_<run>.jsonl`, not the run's, because the evidence a human signs includes the run's provenance tip and an advisory call must not void an approval already written. It charges no budget and seals nothing.
- **Policy:** which models may coordinate is human-owned: the `coordination` capability in `policy/model_registry.yaml`. `LN-AGENTS.md` only chooses among them.

### Choosing models

`/models` shows the catalog. `/agents` shows each agent slot (`design.proposal_b.primary`, `build.primary`, `human_authority.model_assistance`, ...), the model it is bound to, where that runs and which operations it serves. Three commands change that, in design mode:

- `/add_model <provider> <model_id> [capability ...]` adds a model to the catalog. Its family comes from the provider's `family_patterns`, never from what you type. A model the catalog already has (same provider and id) is not added twice: the capabilities you name are claimed for it, which is how a known but unclaimed model becomes bindable. A provider that starts one fixed server (`launch`, the llama.cpp ones) is refused: that is an edit to its launch. A capability is a claim you make, not a measurement; `models test --model M` checks it once the model is bound.
- `/remove_model <model>` removes a model that nothing is bound to, and that is not the coordinator named in `LN-AGENTS.md`.
- `/agents <slot> <model>` binds a slot to a model. A list slot (`model_assistance`) takes several, separated by commas. A slot that holds a non-model (`deterministic_orchestrator`) is not rebound.
- `/agents auto [what you want]` asks the authority to propose bindings, for example `/agents auto use only local models for build`. The authority is the model `LN-AGENTS.md` names as `coordinator` (or its `fallback`), and only if `policy/model_registry.yaml` binds it to `human_authority`, whose `model_assistance` list is the list of models allowed to act as the authority. It reads the catalog and the slots as data and the human-authority spec as reference, replies with one JSON object, and may only name existing slots and catalog models: it cannot add a model, claim a capability or change anything else. This is the one command here that calls a model.

Each command only drafts. Code builds the new text from the command's own fields (for `/agents auto`, from the bindings the authority named, after checking each), parses it and checks that it holds exactly the registry meant, then validates the whole repository with it: R14 independence, R18 capabilities, R17 families, R20 specs and the rest. A change that would break a rule is refused with the rule's own message and nothing is stored. Otherwise the reply shows a diff and an id, and nothing in `policy/` has changed yet. **`/confirm <id>` writes it**, and only you can type that: no reply from a model is ever run as a command. The file is edited as text, so its comments and layout stay. A draft is dropped with `/discard <id>`, and is refused when the registry has changed since it was made.

A confirmed change is an edit to your working tree, not a commit. You review it with `git diff` and commit and push it signed, like any change to policy, and it is covered by CODEOWNERS when it comes in as a pull request. A run keeps the policy it froze at its start, so a change affects only the runs you start afterwards. The bearer token protects this path (whoever holds it can draft and confirm), as it protects every A2A skill.

### Agent specifications

Each `agents/*.md` is the role specification of one entry under `agents` in `policy/model_registry.yaml` (its `spec`). Registry rule R20 requires that every file in `agents/` is the `spec` of exactly one agent, and that an agent with a `spec` names at least one catalog model (`primary`, `escalation`, `model_assistance` and the other backend slots), so a new spec cannot be added without a model. `validate` fails otherwise.

When an operation runs, its agent's spec goes to the model as reference for its role:

- **Claude and agy** get it in the prompt, after the task and before the sealed inputs, inside a frame written by code: the model acts only as that operation's role, the task and the output format win over the spec, and the spec never lifts the no-tools and no-network rules. The design spec orchestrates `A1`–`A4`, for example, but a call performs one operation. agy's 120 KiB prompt cap includes the spec.
- **Pi and Qwen** (the build stages) get the spec as a read-only file, `/agent/spec.md`, and a line in the prompt naming it and its digest. It is read on demand, so it does not use up the 16k context.
- **The decision model** (Clef-Flash at `A2J`) never gets one: it answers in one batch and its threshold was calibrated on the current prompt.

Each spec also opens with a **brief**: about a kilobyte, between `<!-- ln-agents:brief begin -->` and `<!-- ln-agents:brief end -->`, saying what the agent is, which states it covers, what it hands on and what it may never do. The models that talk *about* the agents (the coordinator and the authority behind `/agents auto`) read the brief in place of the file, which is a tenth of the tokens, in a prompt block tagged `<agent_brief>` that carries the digest of the whole spec. A stage's own model still gets the whole spec. A brief is owned like the rest of `agents/` (CODEOWNERS) and changes only through a reviewed commit; no model writes or updates one while ln-agents runs, because a coordinator would trust what it is told about a role. The `learning` and `release` briefs repeat the "built here" status from the note at the top of their file, so the two are edited together. Registry rule R21 requires that every spec in the repository has exactly one brief of at most 2 KiB, so `validate` fails if one is missing, doubled or too long. A run that froze its specs before briefs existed keeps going: R21 is not applied to a run's frozen copy.

The specs go to the provider like the rest of the prompt, so they follow the same data-class rules. A run freezes `agents/*.md` with the graph and policy (`.runs/<run>/frozen/agents/`) and their digests are part of its recorded policy, so editing a spec changes later runs and not the ones in progress. `agents/` is in CODEOWNERS, and an implementation patch may not write to it, because the specs now steer models.

### CI checks

`policy/ci.yaml` lists the checks that run at `E2` (acceptance, MC/DC, optional contract, performance and dead-code checks) and `F1` (integration, mutation). Each runs offline in the bwrap sandbox on a copy of the pinned tree with the patch overlaid, with a toolchain built from that tree's `uv.lock`. A required check that is unset is `not_evaluated`, never passed.

- **MC/DC** uses `pymcdc`, which measures only `unittest.TestCase` tests. Pytest-style tests do not count, so `E2` cannot pass on this repository's own code until it has `unittest` tests; such a run routes `E2` → `X` → `H1`.
- **Mutation** uses `mutmut` with a minimum score of 80.
- `scripts/`, `policy/` and `agents/` are protected paths: an implementation patch may not change them.

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
├── agents/                    # role specifications, one per agent; each has a model (registry rule R20), a brief (R21), and is passed to its model (instructions, not a security boundary)
├── scripts/                   # MC/DC and mutation wrappers run by the CI checks
├── src/agentic_pipeline/      # runner, gates, adapters, sandbox, approvals, release
├── rust/                      # ln-agents: ACP agent and A2A client; MCP tool loop and sandbox launcher to come
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
