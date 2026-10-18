#!/usr/bin/env bash
# One AppVeyor job on Linux or macOS (see appveyor.yml for the variables). Also runs locally:
#   PYTHON_VERSION=3.13 LINT=1 RUST=1 bash ci/appveyor.sh
set -euo pipefail
cd "$(dirname "$0")/.."
export PATH="$HOME/.local/bin:$HOME/.cargo/bin:$PATH"
export UV_PYTHON="${PYTHON_VERSION:-3.13}"

step() { printf '\n== %s\n' "$*"; }

if ! command -v uv >/dev/null; then
  step "install uv"
  curl -LsSf https://astral.sh/uv/install.sh | UV_NO_MODIFY_PATH=1 sh
fi
step "python $UV_PYTHON and dependencies"
uv python install "$UV_PYTHON"
uv sync --all-groups

if [ "${SANDBOX:-}" = 1 ]; then
  # The sandbox tests need user namespaces; recent Ubuntu restricts them with AppArmor.
  step "bwrap sandbox"
  sudo apt-get update -q
  sudo apt-get install -y -q bubblewrap
  sudo sysctl -w kernel.apparmor_restrict_unprivileged_userns=0 || true
fi

if [ "${LINT:-}" = 1 ]; then
  step "lint and format"
  uv run ruff check .
  uv run ruff format --check .
fi

step "test suite"
uv run pytest
step "pipeline graph invariants"
uv run python -m agentic_pipeline validate

if [ "${RUST:-}" = 1 ]; then
  cd rust
  channel=$(sed -n 's/^channel = "\(.*\)"/\1/p' rust-toolchain.toml)
  if ! command -v rustup >/dev/null && command -v cargo >/dev/null; then
    # A machine with a packaged Rust and no rustup: use it rather than install over it.
    step "rust: the installed $(cargo --version) (rust-toolchain.toml pins $channel)"
  else
    if ! command -v rustup >/dev/null; then
      step "install rustup"
      curl --proto '=https' --tlsv1.2 -sSf https://sh.rustup.rs \
        | sh -s -- -y --no-modify-path --profile minimal --default-toolchain none
    fi
    # By name, so an older rustup on the image works too (rust/rust-toolchain.toml pins it).
    step "rust $channel"
    rustup toolchain install "$channel" --profile minimal -c rustfmt -c clippy
  fi
  cargo fmt --check
  cargo clippy --all-targets --locked -- -D warnings
  cargo test --locked
  step "release build and binary size"
  cargo build --release --locked
  ls -l target/release/ln-hub
fi
