//! The terminal UI as the place a turn is shown: updates go to the UI thread, and `/decide` and
//! `/resume` run in this terminal.

use std::path::PathBuf;
use std::sync::mpsc::{Sender, channel};
use std::sync::{Arc, Mutex};

use agent_client_protocol_schema::v1::SessionUpdate;
use ln_a2a::Client;
use ln_acp::human::{Pending, Setup};
use ln_acp::render;
use ln_acp::turn::Host;

use crate::app::Msg;

/// What turns share: the unsigned document `/approval-doc` last wrote.
#[derive(Default)]
pub struct Shared {
    pending: Mutex<Option<Pending>>,
}

pub struct TuiHost {
    pub config: Arc<ln_acp::Config>,
    pub cwd: PathBuf,
    pub msg: Sender<Msg>,
    pub shared: Arc<Shared>,
}

impl TuiHost {
    fn say(&self, text: String) {
        self.update(render::message(text));
    }
}

impl Host for TuiHost {
    fn update(&self, update: SessionUpdate) {
        let _ = self.msg.send(Msg::Update(Box::new(update)));
    }

    fn client(&self) -> Result<Client, String> {
        self.config.client(&self.cwd)
    }

    fn setup(&self) -> &Setup {
        &self.config.human
    }

    fn bind(&self, run: &str) {
        let _ = self.msg.send(Msg::Bound(run.to_string()));
    }

    fn remember(&self, pending: Pending) {
        *self
            .shared
            .pending
            .lock()
            .unwrap_or_else(|e| e.into_inner()) = Some(pending);
    }

    fn pending(&self) -> Option<Pending> {
        self.shared
            .pending
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .clone()
    }

    /// Nothing is written without a `y` from the person at the keyboard; the default is no.
    fn allow_write(&self, run: &str, decision: &str) -> bool {
        let (reply, answer) = channel();
        let title = format!(
            "Write an unsigned {decision} document for run {run}? It approves nothing until you \
             sign it."
        );
        if self.msg.send(Msg::Ask { title, reply }).is_err() {
            return false;
        }
        answer.recv().unwrap_or(false)
    }

    fn picks(&self) -> bool {
        true
    }

    /// The person picks on the UI thread, by key or by mouse; leaving it, or the UI going away,
    /// is no choice.
    fn choose(&self, title: &str, options: &[String], report: Option<&str>) -> Option<usize> {
        let (reply, answer) = channel();
        let pick = Msg::Pick {
            title: title.to_string(),
            options: options.to_vec(),
            report: report.map(str::to_string),
            reply,
        };
        self.msg.send(pick).ok()?;
        answer.recv().ok().flatten()
    }

    fn ask_line(&self, prompt: &str) -> Option<String> {
        let (reply, answer) = channel();
        let line = Msg::Line {
            prompt: prompt.to_string(),
            reply,
        };
        self.msg.send(line).ok()?;
        answer.recv().ok().flatten()
    }

    /// The step runs in this terminal: the UI leaves the screen, the step asks for the run id and
    /// the passphrase itself, and the UI comes back.
    fn hand_over(&self, args: &[String], run: &str, what: &str, manual: &str) {
        let (reply, answer) = channel();
        let request = Msg::Foreground {
            cwd: self.cwd.clone(),
            args: args.to_vec(),
            reply,
        };
        if self.msg.send(request).is_err() {
            return;
        }
        let text = match answer.recv() {
            Ok(Ok(0)) => format!(
                "Back from the terminal step for run `{run}` (you {what}). It ended with status 0. \
                 `/status` shows where the run is and `/advance` continues it.\n"
            ),
            Ok(Ok(code)) => format!(
                "Back from the terminal step for run `{run}` (you {what}). It ended with status \
                 {code}, so it did not complete; what it said is above. `/status` shows where the \
                 run is.\n"
            ),
            Ok(Err(error)) => format!(
                "The terminal step could not start: {error}.\n\nDo it by hand in a terminal \
                 instead:\n\n```sh\n{manual}\n```\n"
            ),
            Err(_) => return,
        };
        self.say(text);
    }
}
