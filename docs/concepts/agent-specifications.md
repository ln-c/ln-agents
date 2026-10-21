# Agent specifications

Each `agents/*.md` is the role specification of one entry under `agents` in `policy/model_registry.yaml` (its `spec`). Registry rule R20 requires that every file in `agents/` is the `spec` of exactly one agent, and that an agent with a `spec` names at least one catalog model (`primary`, `escalation`, `model_assistance` and the other backend slots), so a new spec cannot be added without a model. `validate` fails otherwise.

When an operation runs, its agent's spec goes to the model as reference for its role:

- **Claude and agy** get it in the prompt, after the task and before the sealed inputs, inside a frame written by code: the model acts only as that operation's role, the task and the output format win over the spec, and the spec never lifts the no-tools and no-network rules. The design spec orchestrates `A1`–`A4`, for example, but a call performs one operation. agy's 120 KiB prompt cap includes the spec.
- **Pi and Qwen** (the build stages) get the spec as a read-only file, `/agent/spec.md`, and a line in the prompt naming it and its digest. It is read on demand, so it does not use up the 16k context.
- **The decision model** (Clef-Flash at `A2J`) never gets one: it answers in one batch and its threshold was calibrated on the current prompt.

Each spec also opens with a **brief**: about a kilobyte, between `<!-- ln-agents:brief begin -->` and `<!-- ln-agents:brief end -->`, saying what the agent is, which states it covers, what it hands on and what it may never do. The models that talk *about* the agents (the coordinator and the authority behind `/agents auto`) read the brief in place of the file, which is a tenth of the tokens, in a prompt block tagged `<agent_brief>` that carries the digest of the whole spec. A stage's own model still gets the whole spec. A brief is owned like the rest of `agents/` (CODEOWNERS) and changes only through a reviewed commit; no model writes or updates one while ln-agents runs, because a coordinator would trust what it is told about a role. The `learning` and `release` briefs repeat the "built here" status from the note at the top of their file, so the two are edited together. Registry rule R21 requires that every spec in the repository has exactly one brief of at most 2 KiB, so `validate` fails if one is missing, doubled or too long. A run that froze its specs before briefs existed keeps going: R21 is not applied to a run's frozen copy.

**Which agent's brief the coordinator reads.** At a stop and when you ask a question, the coordinator is given the brief of the agent that covers the run's current node, read from the specs the run froze, with a sentence saying it describes that agent and is not an instruction to the coordinator. A node a model works (A1, A2, E1 and so on) belongs to the agent its `stage_requirements` operations name (`design.proposal_a` is `design`). The human stops have no operation, so the registry lists them under `states` on the agent that covers them: `human_authority` has H1, `release` has R1 and R2, and `learning` has HR, the review of calibration and allowlist proposals. Registry rule R22 checks that every listed state is in the graph, that it is not already covered by `stage_requirements`, that no two agents list it, and that the agent has a spec. A node no agent covers (for example `G`), or an agent whose frozen spec has no brief, gets no brief and the prompt it always had. A run frozen before `states` existed gets a brief at its model states only.

The specs go to the provider like the rest of the prompt, so they follow the same data-class rules. A run freezes `agents/*.md` with the graph and policy (`.runs/<run>/frozen/agents/`) and their digests are part of its recorded policy, so editing a spec changes later runs and not the ones in progress. `agents/` is in CODEOWNERS, and an implementation patch may not write to it, because the specs now steer models.

## The agents

The agent, its specification and the states it covers:

| Agent | Specification | Existing pipeline stages |
|---|---|---|
| Grounding | [`agents/pipeline-grounding-agent.md`](../../agents/pipeline-grounding-agent.md) | START, G; context manifest and evidence |
| Design | [`agents/pipeline-design-agent.md`](../../agents/pipeline-design-agent.md) | A1–A4; independent proposals, critique, synthesis |
| Build | [`agents/pipeline-build-agent.md`](../../agents/pipeline-build-agent.md) | C1–E4; decomposition, contracts, tests, implementation |
| Exception Router | [`agents/pipeline-exception-router.md`](../../agents/pipeline-exception-router.md) | X; evidence-based exception classification and routing |
| Release | [`agents/pipeline-release-agent.md`](../../agents/pipeline-release-agent.md) | F1–RB; CI, merge, deploy authorization, rollback (`D2`, `RB` not built) |
| Learning | [`agents/pipeline-learning-agent.md`](../../agents/pipeline-learning-agent.md) | L1–L3, HR; provenance and gated evaluation |
| Human Authority | [`agents/pipeline-human-authority-agent.md`](../../agents/pipeline-human-authority-agent.md) | H1, DR, BENCH, CHILD; explicit human decisions |
