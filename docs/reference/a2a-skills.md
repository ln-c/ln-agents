# A2A skills

A request is one A2A message with one data part naming a skill:
- `start_run` (`run_id`, `entry_spec`);
- `advance` (`run_id`, or the task's run);
- `status`;
- `evidence`: the evidence, the choices a human can sign, who may sign, and the readable report (its Markdown and the path it was written to);
- `approval_document` (`decision`, `approver`, `reason`, and `candidate` for a design approval);
- `ask` (`question`, and `run_id` unless the task has one): the coordinator's text answer, from the run's Markdown views (see [The coordinator](../concepts/coordinator.md)).
- `models` (no arguments, no run): the model catalog of the repository's registry, the rows `models discover` prints: provider, family, location, capabilities, what each model is bound to and, when bound, its readiness and conformance status. It is read-only and calls no model. If the repository fails `validate`, the reply carries the errors instead of a catalog.
- `agents` (no arguments, no run): which model each agent slot of the registry is bound to and what runs on it, and the registry changes waiting for a human.
- `guideline` (no arguments, no run): the steps from naming a coordinator in `LN-AGENTS.md` to a `/start` that nothing blocks, each with where it stands and what to do (see [Getting started](../getting-started.md#your-first-run-guideline)). Read-only; it calls no model.
- `registry_change` (`kind` and its fields, no run): draft one change to `policy/model_registry.yaml`. `add_model` (`provider`, `model_id`, `capabilities`) adds a model, or claims capabilities for one the catalog already has; `remove_model` (`model`) removes one nothing is bound to; `bind` (`slot`, `models`) binds an agent slot. Code checks the result against every registry rule and stores it with a diff. It writes nothing to `policy/`.
- `propose_mapping` (`intent`, optional; no run): ask the authority model to propose bindings for the agent slots. The model is the one `LN-AGENTS.md` names, and only if the registry binds it to `human_authority`. Code checks every binding and stores what survives as a draft, like `registry_change`; it writes nothing to `policy/`. It calls the model, so it uses its subscription allowance.
- `complete_mapping` (no arguments, no run): if some agent has no model, draft the one change that gives each a model. The authority names a catalog model for each, from the brief of its spec, and code writes the draft; when the authority cannot be used, code gives each the coordinator `LN-AGENTS.md` names, and the reply says which and why. A draft already waiting for the same agents is shown again with no new model call. With every agent mapped it answers with an empty gap list. `start_run` is refused while an agent has no model, so no run directory is created.
- `apply_change` (`change_id`): write a stored draft, if the registry is still the file it was made from and still passes every rule. It takes an id and no text.
- `discard_change` (`change_id`): drop a stored draft.

Each skill calls the same code as `run`. **None of them signs, decides or resumes:** the human still signs with `ssh-keygen` and runs `run decide` or `run resume` (`ln-agents` opens a terminal for that, see [Editor commands](editor-commands.md)). The one policy file they can change is `policy/model_registry.yaml`, and only through `apply_change`: see [Choosing models](../guides/choosing-models.md). Budgets, vetoes, approvers and the graph cannot be reached.

A request is checked before any task is touched. A refused request is a JSON-RPC error and leaves every task as it was. `status`, `evidence`, `ask`, `approval_document` and `models` are answered with a message, even when the request names a task. A message that names a task without a `contextId` gets the task's context.

`advance` streams every provenance event as a WORKING status update, then ends in one of these states:
- `TASK_STATE_INPUT_REQUIRED` when the run awaits a human, with the evidence attached as an artifact;
- `TASK_STATE_AUTH_REQUIRED` when the run paused on `auth_failed` or `quota_exhausted`;
- `TASK_STATE_INPUT_REQUIRED` when the run is otherwise paused, blocked or out of steps (it is resumable);
- `TASK_STATE_COMPLETED` at a terminal state.

Because one A2A state covers several stops, the final status message also carries `{"run_status", "node"}`: `awaiting_human`, `paused`, `blocked`, `out_of_steps`, `stopped` or `finished`. A blocked step spent nothing. Advance retries it, but it blocks again until its cause is fixed. `ln-agents` says a run "needs a human" only for `awaiting_human`.

Tasks are stored under `<runs-dir>/.a2a/tasks` and answered from the run's own state after a restart.
