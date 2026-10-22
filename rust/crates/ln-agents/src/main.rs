//! `ln-agents`: the command line. Bare `ln-agents` opens the terminal UI, `ln-agents -p "<prompt>"`
//! runs one prompt and prints the answer, and the subcommands are the pieces they are made of:
//! `acp` for an editor, and `decide` and `resume`, which only a person at a terminal runs
//! (`harness`, `sandbox` and `status` come with the PRs that implement them).

use std::io::{IsTerminal, Read};
use std::path::PathBuf;
use std::process::ExitCode;
use std::sync::Arc;

mod human;
mod print;

use clap::parser::ValueSource;
use clap::{ArgMatches, Args, CommandFactory, FromArgMatches, Parser, Subcommand, ValueEnum};
use ln_acp::commands::Mode;

#[derive(Parser)]
#[command(name = "ln-agents", version, about, long_about = None)]
struct Cli {
    #[command(flatten)]
    session: Session,
    /// Run one prompt, print the answer and leave; with no PROMPT (or `-`) it is read from stdin.
    /// Nothing is asked of anyone: no document is written, and `/decide` and `/resume` need
    /// `ln-agents` without this flag. Exit status: 0 an answer, 1 the command failed or was
    /// refused, 2 bad usage.
    #[arg(
        short = 'p',
        long = "print",
        value_name = "PROMPT",
        num_args = 0..=1,
        default_missing_value = "-"
    )]
    print: Option<String>,
    /// What the session may do: `status` reads, `design` also starts runs and drafts changes.
    #[arg(long, value_enum, default_value = "status")]
    mode: ModeArg,
    /// Draw in a few rows of the normal screen instead of the full screen: finished messages go
    /// into the terminal's own scrollback, so its search, selection and copy work. Also
    /// LN_AGENTS_NO_ALT_SCREEN=1.
    #[arg(long)]
    no_alt_screen: bool,
    #[command(subcommand)]
    command: Option<Commands>,
}

#[derive(Clone, Copy, ValueEnum)]
enum ModeArg {
    Status,
    Design,
}

impl From<ModeArg> for Mode {
    fn from(mode: ModeArg) -> Mode {
        match mode {
            ModeArg::Status => Mode::Status,
            ModeArg::Design => Mode::Design,
        }
    }
}

/// How `ln-agents` reaches the orchestrator and the human's key: the same for the terminal UI and
/// for `acp`.
#[derive(Args, Clone)]
struct Session {
    /// The orchestrator's A2A endpoint; a literal loopback address only.
    #[arg(long, env = "LN_AGENTS_A2A_URL", default_value = ln_a2a::DEFAULT_URL)]
    a2a_url: String,
    /// The bearer token file [default: <session cwd>/.runs/.a2a/token].
    #[arg(long, env = "LN_AGENTS_TOKEN_FILE")]
    token_file: Option<PathBuf>,
    /// How `/decide` and `/resume` run the orchestrator's CLI, split on spaces
    /// [default: uv run python -m agentic_pipeline].
    #[arg(long, env = "LN_AGENTS_PIPELINE")]
    pipeline: Option<String>,
    /// The private key the human approves with [default: ~/.ssh/id_ed25519_ln_approval].
    #[arg(long, env = "LN_AGENTS_SSH_KEY")]
    ssh_key: Option<PathBuf>,
    /// Where `/resume` finds runs [default: <session cwd>/.runs].
    #[arg(long, env = "LN_AGENTS_RUNS_DIR")]
    runs_dir: Option<PathBuf>,
}

impl Session {
    fn setup(&self) -> ln_acp::human::Setup {
        ln_acp::human::Setup {
            terminal: None,
            pipeline: self
                .pipeline
                .as_deref()
                .map(|p| p.split_whitespace().map(String::from).collect())
                .unwrap_or_default(),
            key: self.ssh_key.clone(),
            runs_dir: self.runs_dir.clone(),
        }
    }
}

#[derive(Subcommand)]
enum Commands {
    /// Serve ACP on stdio for an editor (Zed, JetBrains, ...), driving ln-pipeline over A2A.
    Acp {
        #[command(flatten)]
        session: Session,
        /// The words that open a terminal window for `/decide` and `/resume`, such as `kitty`
        /// or `alacritty -e` [default: $TERMINAL, xdg-terminal-exec, or a known emulator].
        #[arg(long, env = "LN_AGENTS_TERMINAL")]
        terminal: Option<String>,
    },
    /// Sign an approval document with your SSH key and record the decision. For a terminal
    /// you are looking at: `/decide` in `ln-agents` runs it here, and in an editor opens one.
    /// Never run by a model.
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
    /// Resume a paused run in a terminal you are looking at: `/resume` in `ln-agents` runs it
    /// here, and in an editor opens one.
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

/// The flags of the terminal UI that were typed before a subcommand, where they would be ignored.
const UI_FLAGS: [&str; 7] = [
    "a2a_url",
    "token_file",
    "pipeline",
    "ssh_key",
    "runs_dir",
    "mode",
    "no_alt_screen",
];

fn main() -> ExitCode {
    let matches: ArgMatches = Cli::command().get_matches();
    let cli = Cli::from_arg_matches(&matches).unwrap_or_else(|error| error.exit());
    if cli.command.is_some()
        && let Some(flag) = UI_FLAGS
            .iter()
            .find(|id| matches.value_source(id) == Some(ValueSource::CommandLine))
    {
        eprintln!(
            "ln-agents: --{} before a subcommand has no effect; put it after the subcommand",
            flag.replace('_', "-")
        );
        return ExitCode::from(2);
    }
    match cli.command {
        None => match &cli.print {
            Some(prompt) => print(&cli, prompt),
            None => terminal(&cli),
        },
        Some(_) if cli.print.is_some() => {
            eprintln!("ln-agents: -p prints one prompt and cannot be combined with a subcommand");
            ExitCode::from(2)
        }
        Some(Commands::Acp { session, terminal }) => {
            // Refuse a non-loopback endpoint before the editor sends anything.
            if let Err(error) = ln_a2a::loopback_origin(&session.a2a_url) {
                eprintln!("ln-agents acp: {error}");
                return ExitCode::FAILURE;
            }
            let mut setup = session.setup();
            setup.terminal = terminal;
            let config = ln_acp::Config {
                url: session.a2a_url,
                token_file: session.token_file,
                window: Arc::new(human::Terminal(setup.clone())),
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

/// The config for a session in `cwd`, or why the endpoint is refused.
fn config(session: &Session) -> Result<ln_acp::Config, String> {
    ln_a2a::loopback_origin(&session.a2a_url).map_err(|e| e.to_string())?;
    Ok(ln_acp::Config {
        url: session.a2a_url.clone(),
        token_file: session.token_file.clone(),
        human: session.setup(),
        window: Arc::new(ln_acp::human::NoWindow),
    })
}

fn cwd() -> Result<PathBuf, String> {
    std::env::current_dir().map_err(|e| format!("cannot find the current directory: {e}"))
}

/// Bare `ln-agents`: the terminal UI.
fn terminal(cli: &Cli) -> ExitCode {
    let started = config(&cli.session).and_then(|config| {
        ln_tui::needs_terminal()?;
        let options = ln_tui::Options {
            config,
            cwd: cwd()?,
            mode: cli.mode.into(),
            screen: if cli.no_alt_screen || is_set(std::env::var("LN_AGENTS_NO_ALT_SCREEN").ok()) {
                ln_tui::Screen::Inline
            } else {
                ln_tui::Screen::Alternate
            },
            foreground: Arc::new(human::Here),
        };
        ln_tui::run(options)
    });
    match started {
        Ok(()) => ExitCode::SUCCESS,
        Err(error) => {
            eprintln!("ln-agents: {error}");
            ExitCode::FAILURE
        }
    }
}

/// `ln-agents -p`: one prompt, printed.
fn print(cli: &Cli, prompt: &str) -> ExitCode {
    let prompt = if prompt == "-" {
        if std::io::stdin().is_terminal() {
            eprintln!("ln-agents: -p needs a prompt: `-p \"<prompt>\"`, or text on stdin");
            return ExitCode::from(2);
        }
        let mut text = String::new();
        if let Err(error) = std::io::stdin().read_to_string(&mut text) {
            eprintln!("ln-agents: cannot read the prompt from stdin: {error}");
            return ExitCode::from(2);
        }
        text
    } else {
        prompt.to_string()
    };
    match config(&cli.session).and_then(|config| Ok((config, cwd()?))) {
        Ok((config, cwd)) => print::run(&prompt, config, cwd, cli.mode.into()),
        Err(error) => {
            eprintln!("ln-agents: {error}");
            ExitCode::FAILURE
        }
    }
}

/// Whether an environment switch is on: set, and not empty, `0` or `false`.
fn is_set(value: Option<String>) -> bool {
    value.is_some_and(|v| !matches!(v.trim(), "" | "0" | "false" | "no" | "off"))
}

fn exit(code: i32) -> ExitCode {
    u8::try_from(code).map_or(ExitCode::FAILURE, ExitCode::from)
}

#[cfg(test)]
mod tests {
    use super::is_set;

    #[test]
    fn an_environment_switch_is_on_unless_it_is_empty_zero_or_false() {
        assert!(is_set(Some("1".into())));
        assert!(is_set(Some("true".into())));
        assert!(is_set(Some("yes".into())));
        for off in ["", "0", "false", "no", "off", " 0 "] {
            assert!(!is_set(Some(off.into())), "{off:?}");
        }
        assert!(!is_set(None));
    }
}
