# Contributing

This is a policy-driven repository: the state machine, the policy and the schemas decide what the pipeline may do, and a human owns some of those files. Read [`AGENTS.md`](AGENTS.md) first. People and coding agents follow the same rules here.

To report a vulnerability, do not open an issue: see [`SECURITY.md`](SECURITY.md).

## Check your change

[Getting started](docs/getting-started.md#requirements-and-checks) has the requirements and the commands: `uv sync --all-groups`, `pytest`, `validate` and `ruff` for Python, and `cargo fmt`, `clippy` and `test` in `rust/`. Run them all before you push. CI runs the same, and these seven checks must pass before a merge:

| Check | Runs |
|---|---|
| `validate (3.13)`, `validate (3.14)` | ruff, pytest and `validate` on Ubuntu, with `bwrap` for the sandbox tests |
| `portable (windows-latest)`, `portable (macos-latest)` | pytest and `validate`; the sandbox tests skip |
| `rust (ubuntu-latest)`, `rust (windows-latest)`, `rust (macos-latest)` | `cargo fmt`, `clippy`, `test` and a release build |

The control plane runs on Windows and macOS too, so a change outside the sandbox code must not assume POSIX paths or `bwrap`. CodeQL code scanning must also report no error or high-severity alert.

The tests make throwaway git repositories and commit in them. Those commits are signed too if your git config signs every commit, and the tests fail when the key is not available. Run `GIT_CONFIG_GLOBAL=/dev/null uv run pytest` (on Windows, point the variable at an empty file).

## Branches, commits and pull requests

- Branch from an up-to-date `main`, not from another pull request's branch. Keep one purpose per pull request.
- Every commit on every branch needs a verified signature; GitHub rejects an unsigned push. [SSH signing](https://docs.github.com/en/authentication/managing-commit-signature-verification/telling-git-about-your-signing-key) works.
- `main` takes squash merges only, keeps a linear history, and refuses force-pushes and deletion. Write the pull request title and the commit subject with the type prefix the history uses (`feat:`, `fix:`, `docs:`, `refactor:`, `test:`, `ci:`, `build:`, `policy:`).
- In the description, say what the change does and why, which checks you ran, and anything you did not run or touch.
- The maintainer merges. No approving review is required, but GitHub asks `@ln-c` to review a change under a human-owned path (below). Nobody else, and no model, approves or merges on their behalf.

## Human-owned files

[`.github/CODEOWNERS`](.github/CODEOWNERS) names the paths that set what the pipeline may do: `pipeline/`, `policy/`, `agents/`, `schemas/` and `.github/CODEOWNERS` itself. A change to them goes through the maintainer's review.

- Never change a test, a schema or a policy value to make a failing change pass. The tests pin the invariants, for example `tests/test_registry.py` pins the registry rules; list every pinning test a pull request edits in its description, for the code owner.
- The invariants in [`AGENTS.md`](AGENTS.md) are the maintainer's to change, not a contributor's or a model's.

## Working with a coding agent

An agent working in this repository reads [`AGENTS.md`](AGENTS.md) and [`CLAUDE.md`](CLAUDE.md), which set what it may do. A model cannot approve a merge or a deployment, change a budget, clear a hard veto or impersonate human authority.

## Code and docs

- Python 3.13 or later, formatted and linted by `ruff` as `pyproject.toml` configures it. `ln-agents` in `rust/` builds with the toolchain pinned in `rust/rust-toolchain.toml`, with `clippy` warnings as errors.
- GitHub Actions in the workflow are pinned to commit SHAs with the version in a comment. Dependabot opens weekly update pull requests for them and for both lockfiles; they go through review and CI like any other.
- Documentation lives in [`docs/`](docs/README.md), by kind: guides for tasks, concepts that explain, reference for lookup. Put a page where its reader looks for it, and keep relative links working.

## License

The repository is under the [Apache License 2.0](LICENSE). Under its section 5, a contribution you submit is under the same license unless you say otherwise.
