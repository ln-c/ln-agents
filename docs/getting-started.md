# Getting started

## Requirements and checks

Requires Python 3.13+ and [`uv`](https://docs.astral.sh/uv/); `ln-agents` in `rust/` needs `rustup` (it installs the pinned toolchain). The control plane (`validate`, `run init/step/status/approve/decide`, the gates and approvals) runs on Linux, macOS and Windows; CI tests it on all three. Running model stages and CI checks also needs `bubblewrap` (`bwrap`) with unprivileged user namespaces, so for now only on Linux (elsewhere readiness reports the sandbox UNVERIFIED and those stages block), plus `git` and `ssh-keygen` (OpenSSH 8.2+ for `-Y`). Local models run through a llama.cpp `llama-server` or an OpenVINO Model Server on loopback (see `providers` in `policy/model_registry.yaml`). You do not start the llama.cpp servers by hand: each local provider's `launch` in the registry is how dispatch starts it when a stage needs it and it is not running, and it is stopped again after the stage (a server you started yourself is used and left running). Logs go to `<runs-dir>/.servers/<provider>.log`; a server that does not come up in time refuses the stage before anything is spent, and `preflight` shows a stopped one as "started on demand". `launch` must bind the loopback host and port the readiness probe checks (registry rule R19) and pin `-dev Vulkan0` (with `ggml-openvino` installed a bare `llama-server` picks the OpenVINO CPU backend). The decision model Clef-Flash runs on port 8081 with a 8192-token batch, because it answers only on `/v1/systemone` and judges each critique finding at `A2J` in one batch.

```bash
uv sync --all-groups

uv run pytest                                 # tests; the sandbox tests skip where bwrap is unusable
uv run python -m agentic_pipeline validate    # graph, policy and model-registry invariants
uv run ruff check . && uv run ruff format --check .

# ln-agents (rust/, toolchain pinned in rust/rust-toolchain.toml); CI runs it on Linux, Windows and macOS
cd rust && cargo fmt --check && cargo clippy --all-targets --locked -- -D warnings && cargo test --locked
```

## Your first run: `/guideline`

A new user names one coordinator in `LN-AGENTS.md` and types `/guideline`. It walks the steps in the order a person meets them and says where each stands: **done**, **to do** (yours), **automatic** (the pipeline does it when it is needed) or **not checked yet** (an earlier step has to be fixed first). It starts with a verdict, ready or how many steps keep `/start` from working, and ends with notes on what it cannot see.

1. **Name a coordinator in `LN-AGENTS.md`.** It lists the models that can coordinate and shows the file to create, with `<model>` left for you to fill: nothing picks one for you, and the file is never written for you. This step keeps `/start` from working only while an agent has no model, because nothing can be proposed for it without a coordinator. If no model that can coordinate is ready (today only `gemini_3_8_flash_high` declares `coordination`), it shows how to claim the capability for another model on a supported provider: `/add_model <provider> <model_id> coordination`, then `/confirm <id>` (a declared capability, not a measured one). While an agent has no model `/add_model` is refused, and it says to make that one-line edit to `policy/model_registry.yaml` by hand.
2. **The coordinator is ready**, using its own readiness checks. If it is not, views and summaries fall back to the raw record; a start is not blocked.
3. **The coordinator may choose models for you (optional).** It does if the registry binds it to `human_authority`; otherwise code, not a model, gives each agent that has none the coordinator, and `/agents human_authority.model_assistance ...` is the way to bind it.
4. **The repository passes its own checks** (what `python -m agentic_pipeline validate` reports, except R20).
5. **Every agent has a model.** If not: `/start` proposes one for each, `/confirm <id>` writes it, `/start` again.
6. **The project has a commit to pin**, since a run pins the commit it starts from.
7. **Local model servers can be started.** One that is not running is **automatic** if the program and the files its `launch` names exist, because dispatch starts it when a stage needs it and stops it afterwards.
8. **Every stage a run can reach has a ready model.** That is every model stage reachable from `START` without a human decision, in the graph's own order. The fast path leads to the build stages (C1, D1, E1) before anyone decides, so they count; a disabled stage never does.

`/guideline` is read-only, writes nothing and calls no model, so it works before any coordinator exists. It is built from the checks the start gate and dispatch apply, not a second list: the same host probe (one per reply), `evaluate_stage` for each stage, and the refusals dispatch makes about adapters (`dispatch.adapter_blockers`), so it does not say ready while those would refuse. It reads the repository as it is now, not a run's frozen copy, because it is about starting. A local server that is not running is assumed to come up serving only the model its `--alias` names, and the adapters of those models are checked when the server starts, as dispatch does. It vouches for the repository and the host, not for what you will run on them. It cannot see whether the provider tools are signed in or have quota left (the pipeline does not inspect a login, and a call that fails for either pauses the run), nor what the run's own inputs decide: the task class at START, and the data class G gives the project's files, which can keep a stage off the cloud. Those are checked when the run reaches them. A run waits at H1, R1 and R2 for your signed decision; that is by design.
