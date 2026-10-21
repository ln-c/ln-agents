# Command line

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
