//! `ln-hub`: the command line. Subcommands arrive with the PRs that implement them
//! (`acp`, `harness`, `sandbox`, `status`); this skeleton only reports its version.

use clap::Parser;

#[derive(Parser)]
#[command(name = "ln-hub", version, about, long_about = None)]
struct Cli {}

fn main() {
    Cli::parse();
}
