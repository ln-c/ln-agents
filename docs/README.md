# Documentation

Start with [Getting started](getting-started.md). The state table (what is built and what is not) is in the [README](../README.md#status).

## Guides

- [Human decisions](guides/human-decisions.md): read the evidence, sign a decision at `H1`, `R1` or `R2`.
- [Choosing models](guides/choosing-models.md): `/models`, `/agents`, `/add_model` and `/confirm`.

## Concepts

- [The coordinator](concepts/coordinator.md): the model that writes the readable views, summaries and answers.
- [Agent specifications](concepts/agent-specifications.md): what each `agents/*.md` is and what a model is given.
- [Release and learning](concepts/release-and-learning.md): `R1` to `MERGED`, `L1` and `L2`.
- [Phases](concepts/phases.md): the eight groups of states in the graph.

## Reference

- [Command line](reference/cli.md): `validate`, `preflight`, `models`, `run`, `serve a2a`.
- [A2A skills](reference/a2a-skills.md): what the orchestrator serves to other agents.
- [Editor commands](reference/editor-commands.md): `ln-agents acp` and its slash commands.
- [Terminal UI](reference/terminal-ui.md): `ln-agents` and `ln-agents -p`, the same commands in a terminal.
- [CI checks](reference/ci-checks.md): the checks that run at `E2` and `F1`.
- [Repository layout](reference/repository-layout.md): where each kind of file lives.
