# MCP server

`python -m agentic_pipeline serve mcp --profile P` is the local project MCP server (`ln-pipeline`). ln-agents is the MCP host: for one agent's session it starts this server over stdio, and the server offers that agent the tools of profile `P` and nothing else. It serves the project it is started in, read-only. See [Protocols](../concepts/protocols.md) for where MCP sits next to ACP and A2A.

It needs the `mcp` extra (`uv sync --extra mcp`).

```bash
uv run python -m agentic_pipeline serve mcp --profile local_project [--root PROJECT] [--runs-dir D]
```

`--root` is the project to serve (default: the checkout the package is in) and `--runs-dir` is where its runs and the audit log live (default `.runs`). The caller is whoever spawned the process; there is no network listener.

## Profiles

`policy/tools.yaml` (human-owned) lists the profiles. Each has:

- `destination`: where the caller's model runs, in the kinds of `policy/data_classes.yaml` `destinations` (`local`, `internal_service`, `cloud_service`, `third_party`);
- `tools`: the tools it may call. A tool not listed is not registered: it is absent from `tools/list`, and a call to it is refused and logged;
- `limits`: `max_read_bytes` (per read, and four times that for any result) and `max_calls` (per server process).

`validate` checks the file. The tool names are a closed list in `schemas/tools_policy.schema.json`. None approves, signs, decides, resumes or changes policy, budgets or vetoes, so no profile can grant that.

## Tools

All tools are read-only.

| Tool | What it returns |
|---|---|
| `repo_list` (`path`) | The files and directories under a project-relative path. Unreadable entries are not shown. |
| `repo_read` (`path`, `offset`, `length`) | A slice of a text file, at most `max_read_bytes`; `truncated` says there is more. |
| `repo_search` (`query`, `path`, `glob`, `ignore_case`, `max_results`) | Lines containing a plain-text query in the readable text files under a path. |
| `run_status` (`run_id`) | The run's state: node, status, pause cause, budget use, and whether a runner holds it. |
| `run_evidence` (`run_id`) | The evidence report saved for the run's current node, if a person generated one with `/evidence`. Nothing is generated or written. |
| `pipeline_guideline` | Where each step from naming a coordinator to a first run stands for this project. |
| `models` | The model catalog of the project's registry and the agents that have no model. |

## What the server refuses

- **Repository content to a destination that may not have it.** `repo_*` and `run_evidence` return repository content, or run documents that can carry code. A destination with `cloud: true` receives them only if the class of `artifact_defaults.pinned_source_tree` in `policy/data_classes.yaml` is `cloud_allowed`. By default it is `confidential`, so a cloud profile gets no repository tool, and `validate` reports a profile that lists one.
- **Paths outside the project.** A path is relative to the root, with forward slashes: no absolute path, no `..`, no backslash, no NUL. A symlink is followed and the result must still be inside the root.
- **Secrets and run data.** Nothing under `.git`, `.runs`, `.ssh`, `.gnupg`, `.aws`, `.venv`, `node_modules`, `__pycache__` or `target`, and nothing named like a credential file (`.env*`, `id_rsa`, `*.pem`, `*.key`, `*secrets*.yaml`, and the like). A file or report whose text matches a secret pattern (`build_gates.SECRET_PATTERNS`) is withheld, and a search drops such lines and counts them.
- **Binary files, oversized results and calls past `max_calls`.**

These are defaults built from policy that already exists. Path-level data classes, and which agent gets which profile, are not decided yet: they are planned for every agent together.

## Audit

Every call, allowed or denied, is appended to a hash-chained log at `<runs-dir>/.mcp/provenance_mcp.jsonl`: the session, the profile, the digest of `policy/tools.yaml`, the tool, a hash of the arguments (never the arguments), whether it was allowed and a reason code. A call to a tool the profile does not offer is logged as `tool_not_offered`. If the log cannot be written, the call is denied.

Reason codes: `ok`, `session_started`, `tool_not_offered`, `call_limit`, `too_large`, `bad_argument`, `bad_path`, `path_outside_root`, `unreadable_path`, `not_found`, `not_a_file`, `not_a_directory`, `not_text`, `secret_in_content`, `run_unreadable`, `audit_unavailable`.

## Not built yet

Streamable HTTP and OAuth, run control, wiring a tool set into each agent's runtime (the Claude and agy CLIs, and the local-model harness), and the interview as an MCP prompt or skill.
