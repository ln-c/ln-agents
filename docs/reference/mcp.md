# MCP server

`python -m agentic_pipeline serve mcp --profile P` is the local project MCP server (`ln-pipeline`). ln-agents is the MCP host: for one agent's session it starts this server over stdio, and the server offers that agent the tools of profile `P` and nothing else. It serves the project it is started in, read-only. See [Protocols](../concepts/protocols.md) for where MCP sits next to ACP and A2A.

It needs the `mcp` extra (`uv sync --extra mcp`).

```bash
uv run python -m agentic_pipeline serve mcp --profile local_project [--root PROJECT] [--runs-dir D]
uv run python -m agentic_pipeline serve mcp --run R --operation STAGE.OP [--backend MODEL] [--root PROJECT] [--runs-dir D]
```

`--root` is the project to serve (default: the checkout the package is in) and `--runs-dir` is where its runs and the audit log live (default `.runs`). The caller is whoever spawned the process; there is no network listener.

## Two kinds of session

- **Live** (`--profile P`): the project's working tree, for a person, an editor or an agent that is not part of a run. Its files have the class of the source tree (`artifact_defaults.pinned_source_tree`), unless a [path rule](#path-rules) gives one a class of its own.
- **Bound to an operation of a run** (`--run R --operation STAGE.OP`): for a model working inside a run, such as `E1.implementation_patch`. The session reads the run's own **frozen** policy (each file is checked against the digest the run recorded) and takes from the run's registry the operation's `tool_profile` and its `tool_reads`: the sealed artifacts of the run's pinned snapshot it may fetch on request. They are re-hashed when the session starts, and a change stops it. The live checkout is never read, so a file edited after grounding is not seen. The run tools answer only for the bound run. `--backend MODEL` makes the session refuse to start if that model runs outside the host while the profile's destination is inside it.

In a bound session each sealed artifact appears where it sits in the repository: the pinned tree at the root, `applicable_contracts` under `contracts/`, `protected_tests` under `tests/`, and any other artifact under a directory named by its id. Where two overlap, the narrower one serves the path and gives it its own class, so a cloud operation that lists only `applicable_contracts` sees `contracts/` and nothing else. A registry has to name the profile and the artifacts (`tool_profile`, `tool_reads`, see the schema) before an operation can be bound; none does yet.

## Profiles

`policy/tools.yaml` (human-owned) lists the profiles. Each has:

- `destination`: where the caller's model runs, in the kinds of `policy/data_classes.yaml` `destinations` (`local`, `internal_service`, `cloud_service`, `third_party`);
- `tools`: the tools it may call. A tool not listed is not registered: it is absent from `tools/list`, and a call to it is refused and logged;
- `limits`: `max_read_bytes` (per read, and four times that for any result) and `max_calls` (per server process).

`validate` checks the file. The tool names are a closed list in `schemas/tools_policy.schema.json`. None approves, signs, decides, resumes or changes policy, budgets or vetoes, so no profile can grant that.

## What a registry may promise

An operation in `policy/model_registry.yaml` may carry `tool_profile` (a profile above) and `tool_reads` (the sealed artifacts it may fetch through the repository tools); an agent may carry `tool_profile` for work outside a run. `validate` checks them against `policy/tools.yaml` and `policy/data_classes.yaml`, in the repository and in a run's frozen copy:

- the profile exists, and its destination is at least as strict as **every** model the operation (or agent) may run on: a model outside the host needs a destination outside it, so the class gate always applies;
- `tool_reads` name sealed catalog artifacts that the operation does not also deny, does not produce, and does not take from another operation of its own stage, which keeps the two designs independent;
- repository tools and `tool_reads` go together, and when the classes are known a destination outside the host has at least one artifact it may receive;
- an operation with session separation (the design stages) is given no run tools.

At dispatch, an operation that is promised tools is refused, before any call, while its adapter cannot provide them. An adapter that starts its model with an MCP configuration declares `provides_tools = True`; none does yet, so no operation can opt in until one is changed.

## Path rules

`artifact_defaults` in `policy/data_classes.yaml` gives a whole artifact one class, and the source tree is `confidential`, so by itself a destination outside the host is sent none of the repository. `path_rules` (human-owned, optional) refine that for the files of the repository, so a project can let a cloud model read its documentation and keep its code on the host:

```yaml
path_rules:
  - {path: "docs/private/**", class: confidential}   # listed first: it wins over the next rule
  - {path: "docs/**",         class: internal}
  - {path: "README.md",       class: internal}
```

- **The first rule that covers a file gives it its class.** A file no rule covers keeps its artifact's class. A rule can lower a class (`docs/**`) or raise one (`docs/private/**`).
- **A `path` is relative to the project root, with `/`.** Each segment is a pattern: `*`, `?` and `[...]` match inside one segment, and case is significant. A segment that is exactly `**` matches any number of segments, none included, so `docs/**` covers `docs` and everything below it, and `**/*.md` every Markdown file. `validate` rejects an absolute path, `..`, a backslash, an empty segment (`docs/`), a `**` that is only part of a segment, a class the file does not define, an entry with other keys, and a path that repeats an earlier rule.
- **They apply to the files of the repository:** the live checkout, and in a bound session the pinned tree, `applicable_contracts` and `protected_tests`, by their place in the repository (the contracts are `contracts/...`). Any other sealed artifact, which sits under its id, keeps its class whole. A caller inside the host is not held by the rules.
- **Reading, listing and searching all follow them.** A file is read only if its class is `cloud_allowed` for a destination outside the host, and the class is judged before the path is looked up, so a refusal does not say whether the path exists. A file reached through a symlink must be allowed both under the name it was asked for and under the file the link leads to. A directory is listed only if it holds a file the caller may be sent, however deep; a search opens only such files and does not count the others. The repository tools are offered once some rule gives the caller something to read.
- **Case is read both ways.** A file system that ignores case could serve `notes/secret.md` as `notes/SECRET.md`, so a path is judged by the first rule that covers it as written and by the first that covers it ignoring case, and both must allow it.
- **What a rule cannot open:** the confinement below (secrets, run data, paths outside the project) is checked first, and `run_evidence`, whose saved reports can carry code, still needs the class of the whole source tree to be allowed.

A rule is the policy's own statement that those files may leave the host, so it is a reviewed, human-owned change. `validate` counts a rule as something a cloud destination may receive from a pinned artifact only if the rule can reach where that artifact sits in the repository (the tree at the root, the contracts under `contracts/`, the tests under `tests/`). This repository has no rules yet.

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

- **Files to a destination that may not have them.** A destination with `cloud: true` is returned a file only if its class is `cloud_allowed`; listings and searches leave the others out. A file has its mount's class, or the one a [path rule](#path-rules) gives it. In a live session the mount's class is `artifact_defaults.pinned_source_tree` (by default `confidential`), so with no rule a cloud profile is offered no repository tool, and the audit log lists what was withheld. `run_evidence` returns run documents that can carry code, so it needs the same class to be allowed.
- **Paths outside the project.** A path is relative to the root, with forward slashes: no absolute path, no `..`, no backslash, no NUL. A symlink is followed and the result must still be inside the root.
- **Secrets and run data.** Nothing under `.git`, `.runs`, `.ssh`, `.gnupg`, `.aws`, `.venv`, `node_modules`, `__pycache__` or `target`, and nothing named like a credential file (`.env*`, `id_rsa`, `*.pem`, `*.key`, `*secrets*.yaml`, and the like). A file or report whose text matches a secret pattern (`build_gates.SECRET_PATTERNS`) is withheld, and a search drops such lines and counts them.
- **Binary files, oversized results and calls past `max_calls`.**

## Audit

Every call, allowed or denied, is appended to a hash-chained log at `<runs-dir>/.mcp/provenance_mcp.jsonl`: the session, the profile, the digest of `policy/tools.yaml`, the tool, a hash of the arguments (never the arguments), whether it was allowed and a reason code. A call to a tool the profile does not offer is logged as `tool_not_offered`. If the log cannot be written, the call is denied.

In a bound session every record also carries the run, the operation and the artifacts. Reason codes: `ok`, `session_started`, `tool_not_offered`, `call_limit`, `too_large`, `bad_argument`, `bad_path`, `path_outside_root`, `unreadable_path`, `class_not_allowed`, `not_found`, `not_a_file`, `not_a_directory`, `not_text`, `secret_in_content`, `other_run`, `run_unreadable`, `audit_unavailable`.

## Not built yet

Streamable HTTP and OAuth, run control, the path-rule values for this repository and the registry bindings that give operations their profiles, wiring a tool set into each agent's runtime (the Claude and agy CLIs, and the local-model harness), grounding recording a class per file in the manifest, and the interview as an MCP prompt or skill.
