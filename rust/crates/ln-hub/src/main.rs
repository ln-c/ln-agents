//! `ln-hub`: the command line. Subcommands arrive with the PRs that implement them
//! (`acp` now; `harness`, `sandbox`, `status` later).

use std::path::PathBuf;
use std::process::ExitCode;

use clap::{Parser, Subcommand};

#[derive(Parser)]
#[command(name = "ln-hub", version, about, long_about = None)]
struct Cli {
    #[command(subcommand)]
    command: Option<Commands>,
}

#[derive(Subcommand)]
enum Commands {
    /// Serve ACP on stdio for an editor (Zed, JetBrains, ...), driving ln-pipeline over A2A.
    Acp {
        /// The orchestrator's A2A endpoint; a literal loopback address only.
        #[arg(long, env = "LN_HUB_A2A_URL", default_value = ln_a2a::DEFAULT_URL)]
        a2a_url: String,
        /// The bearer token file [default: <session cwd>/.runs/.a2a/token].
        #[arg(long, env = "LN_HUB_TOKEN_FILE")]
        token_file: Option<PathBuf>,
    },
}

fn main() -> ExitCode {
    let cli = Cli::parse();
    match cli.command {
        None => {
            eprintln!("ln-hub: no command; see `ln-hub --help`");
            ExitCode::FAILURE
        }
        Some(Commands::Acp {
            a2a_url,
            token_file,
        }) => {
            // Refuse a non-loopback endpoint before the editor sends anything.
            if let Err(error) = ln_a2a::loopback_origin(&a2a_url) {
                eprintln!("ln-hub acp: {error}");
                return ExitCode::FAILURE;
            }
            let config = ln_acp::Config {
                url: a2a_url,
                token_file,
            };
            ln_acp::serve(std::io::stdin().lock(), std::io::stdout(), config);
            ExitCode::SUCCESS
        }
    }
}
