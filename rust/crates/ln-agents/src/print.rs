//! `ln-agents -p "<prompt>"`: one turn, printed. The reply goes to stdout as the Markdown the
//! orchestrator's replies are; the stages and model calls of a run, and any error, go to stderr,
//! so a pipe gets the answer alone. Nothing is asked of anyone, so nothing is written or signed
//! here: `/approval-doc` is not written, and `/decide` and `/resume` need a person at a
//! terminal.

use std::io::Write;
use std::path::PathBuf;
use std::process::ExitCode;
use std::sync::Mutex;

use agent_client_protocol_schema::v1::{
    ContentBlock, PlanEntryStatus, SessionUpdate, ToolCallStatus,
};
use ln_a2a::Client;
use ln_acp::commands::Mode;
use ln_acp::human::{Pending, Setup};
use ln_acp::turn::{Host, Prompt};

struct Printing {
    config: ln_acp::Config,
    cwd: PathBuf,
    /// The stage last announced on stderr, so each is announced once.
    stage: Mutex<String>,
}

impl Host for Printing {
    fn update(&self, update: SessionUpdate) {
        match update {
            SessionUpdate::AgentMessageChunk(chunk) => {
                let text = match chunk.content {
                    ContentBlock::Text(t) => t.text,
                    ContentBlock::ResourceLink(link) => format!("[{}]({})", link.name, link.uri),
                    _ => return,
                };
                let mut out = std::io::stdout().lock();
                let _ = out.write_all(text.as_bytes());
                let _ = out.flush();
            }
            SessionUpdate::Plan(plan) => {
                let working = plan
                    .entries
                    .iter()
                    .rfind(|e| e.status == PlanEntryStatus::InProgress)
                    .map(|e| e.content.clone());
                if let Some(stage) = working {
                    let mut last = self.stage.lock().unwrap_or_else(|e| e.into_inner());
                    if *last != stage {
                        eprintln!("[stage] {stage}");
                        *last = stage;
                    }
                }
            }
            SessionUpdate::ToolCall(call) if call.status == ToolCallStatus::InProgress => {
                eprintln!("[model] {}", call.title);
            }
            _ => {}
        }
    }

    fn client(&self) -> Result<Client, String> {
        self.config.client(&self.cwd)
    }

    fn setup(&self) -> &Setup {
        &self.config.human
    }

    fn bind(&self, _run: &str) {}

    fn remember(&self, _pending: Pending) {}

    fn pending(&self) -> Option<Pending> {
        None
    }

    fn allow_write(&self, _run: &str, _decision: &str) -> bool {
        false
    }

    fn hand_over(&self, _args: &[String], run: &str, what: &str, manual: &str) {
        eprintln!(
            "This needs a person at a terminal (run `{run}`: you {what}). Run `ln-agents` and use \
             the command there, or do it by hand:\n\n{manual}"
        );
    }

    fn fail(&self, text: String) {
        eprint!("{}", text.trim_start_matches('\n'));
    }
}

/// Run `prompt` as one turn in `mode` and print what it says.
pub fn run(prompt: &str, config: ln_acp::Config, cwd: PathBuf, mode: Mode) -> ExitCode {
    let host = Printing {
        config,
        cwd: cwd.clone(),
        stage: Mutex::new(String::new()),
    };
    let (turn, _cancel) = Prompt::new(host, cwd, None);
    let outcome = turn.run_outcome(prompt, mode);
    let _ = std::io::stdout().flush();
    if outcome.failed {
        ExitCode::FAILURE
    } else {
        ExitCode::SUCCESS
    }
}
