# One AppVeyor job on Windows (see appveyor.yml for the variables). Also runs locally:
#   $env:PYTHON_VERSION = "3.13"; $env:RUST = "1"; ./ci/appveyor.ps1
$ErrorActionPreference = "Stop"
Set-Location (Join-Path $PSScriptRoot "..")
$env:Path = "$env:USERPROFILE\.local\bin;$env:USERPROFILE\.cargo\bin;$env:Path"
$env:UV_PYTHON = if ($env:PYTHON_VERSION) { $env:PYTHON_VERSION } else { "3.13" }

# Run a native command and stop on a non-zero exit (PowerShell does not by itself).
function Invoke-Checked {
    param([Parameter(Mandatory)][string]$Command, [Parameter(ValueFromRemainingArguments)][string[]]$Arguments)
    & $Command @Arguments
    if ($LASTEXITCODE -ne 0) { throw "$Command $Arguments exited with $LASTEXITCODE" }
}
function Step($text) { Write-Host "`n== $text" }

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    Step "install uv"
    $env:UV_NO_MODIFY_PATH = "1"
    Invoke-RestMethod https://astral.sh/uv/install.ps1 | Invoke-Expression
}
Step "python $env:UV_PYTHON and dependencies"
Invoke-Checked uv python install $env:UV_PYTHON
Invoke-Checked uv sync --all-groups

if ($env:LINT -eq "1") {
    Step "lint and format"
    Invoke-Checked uv run ruff check .
    Invoke-Checked uv run ruff format --check .
}

Step "test suite"
Invoke-Checked uv run pytest
Step "pipeline graph invariants"
Invoke-Checked uv run python -m agentic_pipeline validate

if ($env:RUST -eq "1") {
    if (-not (Get-Command rustup -ErrorAction SilentlyContinue)) {
        Step "install rustup"
        $init = Join-Path $env:TEMP "rustup-init.exe"
        Invoke-WebRequest https://win.rustup.rs/x86_64 -OutFile $init
        Invoke-Checked $init -y --no-modify-path --profile minimal --default-toolchain none
    }
    Set-Location rust
    # By name, so an older rustup on the image works too (rust/rust-toolchain.toml pins it).
    $channel = (Select-String -Path rust-toolchain.toml -Pattern '^channel = "(.*)"').Matches[0].Groups[1].Value
    Step "rust $channel"
    Invoke-Checked rustup toolchain install $channel --profile minimal -c rustfmt -c clippy
    Invoke-Checked cargo fmt --check
    Invoke-Checked cargo clippy --all-targets --locked -- -D warnings
    Invoke-Checked cargo test --locked
    Step "release build and binary size"
    Invoke-Checked cargo build --release --locked
    Get-Item target/release/ln-hub.exe | Format-Table Name, Length
}
