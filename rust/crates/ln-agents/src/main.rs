//! `ln-agents`: the command line. Subcommands arrive with the PRs that implement them
//! (`acp`, `decide` and `resume` now; `harness`, `sandbox`, `status` later).

use std::path::PathBuf;
use std::process::ExitCode;

mod human;

use clap::{Parser, Subcommand};

#[derive(Parser)]
#[command(name = "ln-agents", version, about, long_about = None)]
struct Cli {
    #[command(subcommand)]
    command: Option<Commands>,
}

#[derive(Subcommand)]
enum Commands {
    /// Serve ACP on stdio for an editor (Zed, JetBrains, ...), driving ln-pipeline over A2A.
    Acp {
        /// The orchestrator's A2A endpoint; a literal loopback address only.
        #[arg(long, env = "LN_AGENTS_A2A_URL", default_value = ln_a2a::DEFAULT_URL)]
        a2a_url: String,
        /// The bearer token file [default: <session cwd>/.runs/.a2a/token].
        #[arg(long, env = "LN_AGENTS_TOKEN_FILE")]
        token_file: Option<PathBuf>,
        /// The words that open a terminal window for `/decide` and `/resume`, such as `kitty`
        /// or `alacritty -e` [default: $TERMINAL, xdg-terminal-exec, or a known emulator].
        #[arg(long, env = "LN_AGENTS_TERMINAL")]
        terminal: Option<String>,
        /// How that window runs the orchestrator's CLI, split on spaces
        /// [default: uv run python -m agentic_pipeline].
        #[arg(long, env = "LN_AGENTS_PIPELINE")]
        pipeline: Option<String>,
        /// The private key the human approves with [default: ~/.ssh/id_ed25519_ln_approval].
        #[arg(long, env = "LN_AGENTS_SSH_KEY")]
        ssh_key: Option<PathBuf>,
        /// Where `/resume` finds runs [default: <session cwd>/.runs].
        #[arg(long, env = "LN_AGENTS_RUNS_DIR")]
        runs_dir: Option<PathBuf>,
    },
    /// Sign an approval document with your SSH key and record the decision. For a terminal
    /// you are looking at: `/decide` in an editor opens one. Never run by a model.
    Decide {
        run: String,
        #[arg(long)]
        approval: PathBuf,
        /// The ssh-keygen -Y namespace from policy/approvers.yaml.
        #[arg(long)]
        namespace: String,
        #[arg(long)]
        key: Option<PathBuf>,
        #[arg(long)]
        runs_dir: PathBuf,
        /// The orchestrator's CLI, after `--`.
        #[arg(last = true)]
        pipeline: Vec<String>,
    },
    /// Resume a paused run in a terminal you are looking at: `/resume` in an editor opens one.
    Resume {
        run: String,
        #[arg(long)]
        by: String,
        #[arg(long)]
        reason: String,
        #[arg(long)]
        runs_dir: PathBuf,
        /// The orchestrator's CLI, after `--`.
        #[arg(last = true)]
        pipeline: Vec<String>,
    },
}

fn main() -> ExitCode {
    let cli = Cli::parse();
    match cli.command {
        None => {
            eprintln!("ln-agents: no command; see `ln-agents --help`");
            ExitCode::FAILURE
        }
        Some(Commands::Acp {
            a2a_url,
            token_file,
            terminal,
            pipeline,
            ssh_key,
            runs_dir,
        }) => {
            // Refuse a non-loopback endpoint before the editor sends anything.
            if let Err(error) = ln_a2a::loopback_origin(&a2a_url) {
                eprintln!("ln-agents acp: {error}");
                return ExitCode::FAILURE;
            }
            let setup = ln_acp::human::Setup {
                terminal,
                pipeline: pipeline
                    .map(|p| p.split_whitespace().map(String::from).collect())
                    .unwrap_or_default(),
                key: ssh_key,
                runs_dir,
            };
            let config = ln_acp::Config {
                url: a2a_url,
                token_file,
                window: std::sync::Arc::new(human::Terminal(setup.clone())),
                human: setup,
            };
            ln_acp::serve(std::io::stdin().lock(), std::io::stdout(), config);
            ExitCode::SUCCESS
        }
        Some(Commands::Decide {
            run,
            approval,
            namespace,
            key,
            runs_dir,
            pipeline,
        }) => {
            let decision = human::Decision {
                run: &run,
                approval: &approval,
                namespace: &namespace,
                key: key.as_deref(),
                runs_dir: &runs_dir,
                pipeline: &pipeline,
            };
            exit(human::decide(&decision, &mut human::Tty))
        }
        Some(Commands::Resume {
            run,
            by,
            reason,
            runs_dir,
            pipeline,
        }) => {
            let resumption = human::Resumption {
                run: &run,
                by: &by,
                reason: &reason,
                runs_dir: &runs_dir,
                pipeline: &pipeline,
            };
            exit(human::resume(&resumption, &mut human::Tty))
        }
    }
}

fn exit(code: i32) -> ExitCode {
    u8::try_from(code).map_or(ExitCode::FAILURE, ExitCode::from)
}
