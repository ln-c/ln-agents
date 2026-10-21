//! One prompt turn: a slash command run as code over one A2A skill of `ln-pipeline`, for any
//! front end. The ACP agent (an editor) and the terminal UI each implement [`Host`], which is
//! everything a turn needs from the place it is shown: where updates go, which client to use,
//! the session's bound run and unsigned document, the permission to write one, and how the
//! human reaches `/decide` and `/resume`. Nothing here approves, decides or resumes a run.

use std::path::{Path, PathBuf};
use std::sync::mpsc::{Receiver, Sender, channel};
use std::sync::{Arc, Mutex, MutexGuard};
use std::thread;

use agent_client_protocol_schema::v1::{SessionUpdate, StopReason};
use ln_a2a::types::{SendMessageResponse, StreamResponse, Task};
use ln_a2a::{Client, Request};
use serde_json::Value;

use crate::commands::{self, Command, Mode};
use crate::{human, render};

/// What a turn needs from the front end that shows it.
pub trait Host {
    /// Show one update: a message chunk, the plan, a tool call.
    fn update(&self, update: SessionUpdate);
    /// The client for the orchestrator, or why there is none.
    fn client(&self) -> Result<Client, String>;
    /// The settings the command lines of `/decide` and `/resume` are built from.
    fn setup(&self) -> &human::Setup;
    /// The session now follows this run.
    fn bind(&self, run: &str);
    /// Keep the unsigned document `/approval-doc` wrote, for `/decide`.
    fn remember(&self, pending: human::Pending);
    /// The unsigned document `/approval-doc` last wrote in this session.
    fn pending(&self) -> Option<human::Pending>;
    /// Ask whether to write an unsigned document for `decision`: allow or reject this once.
    fn allow_write(&self, run: &str, decision: &str) -> bool;
    /// Put the human where they sign or confirm: `args` are the words after `ln-agents`,
    /// `what` says what they do there, and `manual` is how to do it by hand. The host says what
    /// happened.
    fn hand_over(&self, args: &[String], run: &str, what: &str, manual: &str);
    /// Say why a turn failed: the text was refused, or the command could not be carried out.
    fn fail(&self, text: String) {
        self.update(render::message(text));
    }
}

pub(crate) enum Signal {
    Event(Box<ln_a2a::Result<StreamResponse>>),
    End,
    Cancel,
}

/// The data part of a task-less skill reply.
pub(crate) fn skill_reply(client: &Client, request: &Request) -> ln_a2a::Result<Value> {
    match client.send(request)? {
        SendMessageResponse::Message(m) => ln_a2a::data(&m.parts).cloned().ok_or_else(|| {
            ln_a2a::Error::Protocol(format!("{} reply has no data", request.skill()))
        }),
        SendMessageResponse::Task(_) => Err(ln_a2a::Error::Protocol(format!(
            "{} answered with a task, not a message",
            request.skill()
        ))),
    }
}

/// One task per run: the one whose status changed last, by run id.
pub fn latest_per_run(tasks: Vec<Task>) -> Vec<(String, Task)> {
    let mut latest: Vec<(String, Task)> = Vec::new();
    for task in tasks {
        let Some(run) = ln_a2a::run_id(&task).map(str::to_string) else {
            continue;
        };
        match latest.iter_mut().find(|(r, _)| *r == run) {
            Some(slot) if slot.1.status.timestamp <= task.status.timestamp => slot.1 = task,
            Some(_) => {}
            None => latest.push((run, task)),
        }
    }
    latest.sort_by(|a, b| a.0.cmp(&b.0));
    latest
}

/// One prompt turn, on its own thread so the reader keeps reading (cancel, permission replies).
pub struct Prompt<H> {
    host: H,
    cwd: PathBuf,
    run: Option<String>,
    tx: Sender<Signal>,
    rx: Receiver<Signal>,
}

/// Cancels the turn of the [`Prompt`] it came with: the A2A task is cancelled, which stops the run
/// after its current step.
#[derive(Debug, Clone)]
pub struct Cancel(Sender<Signal>);

impl Cancel {
    pub fn cancel(&self) {
        let _ = self.0.send(Signal::Cancel);
    }
}

/// How a turn ended.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Outcome {
    pub stop: StopReason,
    /// The text was refused or the command could not be carried out.
    pub failed: bool,
}

enum Ended {
    Done,
    Cancelled,
}

impl<H: Host> Prompt<H> {
    /// A turn for the session `host` shows, in `cwd`, bound to `run` if any.
    pub fn new(host: H, cwd: PathBuf, run: Option<String>) -> (Self, Cancel) {
        let (tx, rx) = channel();
        let cancel = Cancel(tx.clone());
        (Self::with_channel(host, cwd, run, tx, rx), cancel)
    }

    pub(crate) fn with_channel(
        host: H,
        cwd: PathBuf,
        run: Option<String>,
        tx: Sender<Signal>,
        rx: Receiver<Signal>,
    ) -> Self {
        Prompt {
            host,
            cwd,
            run,
            tx,
            rx,
        }
    }

    fn say(&self, text: impl Into<String>) {
        self.host.update(render::message(text));
    }

    fn cancelled(&self) -> bool {
        matches!(self.rx.try_recv(), Ok(Signal::Cancel))
    }

    /// Run the prompt's text as one command and say how the turn ended.
    pub fn run(&self, text: &str, mode: Mode) -> StopReason {
        self.run_outcome(text, mode).stop
    }

    /// Like [`Prompt::run`], and also whether the turn failed: the text was refused, or the
    /// command could not be carried out (what went wrong was said as a message).
    pub fn run_outcome(&self, text: &str, mode: Mode) -> Outcome {
        let mut failed = false;
        let ended = match commands::parse(text, mode) {
            Err(refusal) => {
                failed = true;
                self.host.fail(format!("{refusal}\n"));
                Ok(Ended::Done)
            }
            Ok(command) => self.execute(command, mode),
        };
        let stop = match ended {
            Ok(Ended::Done) if !self.cancelled() => StopReason::EndTurn,
            Ok(_) => StopReason::Cancelled,
            Err(error) => {
                failed = true;
                self.host.fail(format!("\n{error}\n"));
                if self.cancelled() {
                    StopReason::Cancelled
                } else {
                    StopReason::EndTurn
                }
            }
        };
        Outcome { stop, failed }
    }

    fn the_run(&self, given: Option<String>) -> Result<String, String> {
        given.or_else(|| self.run.clone()).ok_or_else(|| {
            "No run in this session: name one (`/status <run>`), pick one from `/runs`, or \
             `/start` one."
                .to_string()
        })
    }

    fn bind(&self, run: &str) {
        self.host.bind(run);
    }

    fn execute(&self, command: Command, mode: Mode) -> Result<Ended, String> {
        let client = || self.host.client();
        let a2a = |e: ln_a2a::Error| format!("orchestrator: {e}");
        match command {
            Command::Help => self.say(commands::help(mode)),
            Command::HumanOnly(name) => self.say(format!(
                "`/{name}` is not something ln-agents does.{}\n",
                commands::HUMAN_ONLY
            )),
            Command::Runs => {
                let tasks = client()?.list_tasks().map_err(a2a)?;
                let runs = latest_per_run(tasks);
                if runs.is_empty() {
                    self.say("No runs have been driven over A2A yet (runs made with the CLI are not listed).\n");
                } else {
                    let mut text =
                        String::from("| run | task state | last status |\n|---|---|---|\n");
                    for (run, task) in runs {
                        let why = task
                            .status
                            .message
                            .as_ref()
                            .map(ln_a2a::text)
                            .unwrap_or_default();
                        let why: String = why
                            .lines()
                            .next()
                            .unwrap_or_default()
                            .chars()
                            .take(100)
                            .collect();
                        text.push_str(&format!("| `{run}` | {:?} | {why} |\n", task.status.state));
                    }
                    self.say(text);
                }
            }
            Command::Status { run } => {
                let run = self.the_run(run)?;
                let status = skill_reply(&client()?, &Request::status(&run)).map_err(a2a)?;
                self.bind(&run);
                self.say(render::status_text(&status));
            }
            Command::Evidence { run } => {
                let run = self.the_run(run)?;
                let view = skill_reply(&client()?, &Request::evidence(&run)).map_err(a2a)?;
                for update in render::view_updates(&view) {
                    self.host.update(update);
                }
            }
            Command::Models => {
                let catalog = skill_reply(&client()?, &Request::models()).map_err(a2a)?;
                self.say(render::models_text(&catalog));
            }
            Command::Agents => {
                let reply = skill_reply(&client()?, &Request::agents()).map_err(a2a)?;
                self.say(render::agents_text(&reply));
            }
            Command::Guideline => {
                let reply = skill_reply(&client()?, &Request::guideline()).map_err(a2a)?;
                self.say(render::guideline_text(&reply));
            }
            Command::AddModel {
                provider,
                model_id,
                capabilities,
            } => {
                let request = Request::add_model(&provider, &model_id, &capabilities);
                let reply = skill_reply(&client()?, &request).map_err(a2a)?;
                self.say(render::change_text(&reply));
            }
            Command::RemoveModel { model } => {
                let reply = skill_reply(&client()?, &Request::remove_model(&model)).map_err(a2a)?;
                self.say(render::change_text(&reply));
            }
            Command::Bind { slot, models } => {
                let reply = skill_reply(&client()?, &Request::bind(&slot, &models)).map_err(a2a)?;
                self.say(render::change_text(&reply));
            }
            Command::ProposeMapping { intent } => {
                let request = Request::propose_mapping(&intent);
                let reply = skill_reply(&client()?, &request).map_err(a2a)?;
                self.say(render::change_text(&reply));
            }
            Command::Confirm { id } => {
                let reply = skill_reply(&client()?, &Request::apply_change(&id)).map_err(a2a)?;
                self.say(render::applied_text(&reply));
            }
            Command::Discard { id } => {
                let reply = skill_reply(&client()?, &Request::discard_change(&id)).map_err(a2a)?;
                self.say(render::discarded_text(&reply));
            }
            Command::Ask { question } => {
                let run = self.the_run(None)?;
                let request = Request::ask(Some(&run), &question);
                let reply = skill_reply(&client()?, &request).map_err(a2a)?;
                self.say(render::answer_text(&reply));
            }
            Command::Start { run, entry_spec } => {
                // No run starts while an agent has no model: the server refuses it, and this asks
                // first so that the reply is a proposal the human can confirm.
                let gate = skill_reply(&client()?, &Request::complete_mapping()).map_err(a2a)?;
                if !render::gaps_of(&gate).is_empty() {
                    self.say(render::gap_text(&gate));
                    return Ok(Ended::Done);
                }
                let spec = read_entry_spec(&self.cwd, &entry_spec)?;
                return self.stream(&client()?, &Request::start_run(&run, spec), &run);
            }
            Command::Advance { run } => {
                let run = self.the_run(run)?;
                return self.stream(&client()?, &Request::advance(&run), &run);
            }
            Command::ApprovalDoc {
                decision,
                candidate,
                approver,
                reason,
            } => {
                let run = self.the_run(None)?;
                if !self.host.allow_write(&run, &decision) {
                    self.say("Not written.\n");
                    return Ok(Ended::Done);
                }
                let request = Request::approval_document(
                    &run,
                    &decision,
                    &approver,
                    &reason,
                    candidate.as_deref(),
                );
                let reply = skill_reply(&client()?, &request).map_err(a2a)?;
                self.say(render::approval_text(&reply));
                match human::Pending::from_reply(&run, &reply) {
                    Ok(pending) => self.remember(pending),
                    Err(error) => {
                        self.say(format!("\n`/decide` cannot use this document: {error}.\n"))
                    }
                }
            }
            Command::Decide => {
                let run = self.the_run(None)?;
                let pending = self.pending_for(&run)?;
                if !pending.document.is_file() {
                    return Err(format!(
                        "The document {} is not on disk any more; write it again with \
                         `/approval-doc`.",
                        pending.document.display()
                    ));
                }
                let setup = self.host.setup();
                let args = human::decide_args(setup, &run, &pending.document, &pending.namespace)?;
                let manual = format!("{}\n{}", pending.sign, pending.then);
                self.open_window(&args, &run, "sign and record your decision", &manual);
            }
            Command::Resume { by, reason } => {
                let run = self.the_run(None)?;
                let setup = self.host.setup();
                let args = human::resume_args(setup, &self.cwd, &run, &by, &reason);
                let mut hand = setup.pipeline.clone();
                if hand.is_empty() {
                    hand = human::DEFAULT_PIPELINE
                        .iter()
                        .map(|w| w.to_string())
                        .collect();
                }
                hand.extend(
                    ["run", "resume", &run, "--by", &by, "--reason", &reason].map(String::from),
                );
                let manual = human::display(&hand);
                self.open_window(&args, &run, "confirm the resume", &manual);
            }
        }
        Ok(Ended::Done)
    }

    fn remember(&self, pending: human::Pending) {
        self.host.remember(pending);
    }

    fn pending_for(&self, run: &str) -> Result<human::Pending, String> {
        self.host
            .pending()
            .filter(|pending| pending.run == run)
            .ok_or_else(|| {
                format!(
                    "No unsigned document for run `{run}` in this session: write one with \
                     `/approval-doc <decision> <approver> <reason>` first."
                )
            })
    }

    /// Put the human where they sign or confirm, and say what to do there.
    fn open_window(&self, args: &[String], run: &str, what: &str, manual: &str) {
        self.host.hand_over(args, run, what, manual);
    }

    /// Stream one task, forwarding its events, until it stops or the turn is cancelled. The
    /// request itself is made on the reader thread, so a cancel is answered at once even while
    /// the server is still starting the run.
    fn stream(&self, client: &Client, request: &Request, run: &str) -> Result<Ended, String> {
        self.bind(run);
        let cancel = Arc::new(Mutex::new(Cancellation::default()));
        let reader = (
            self.tx.clone(),
            Arc::clone(&cancel),
            client.clone(),
            request.clone(),
        );
        thread::spawn(move || {
            let (tx, cancel, client, request) = reader;
            read_stream(&client, &request, &tx, &cancel);
        });
        let mut turn = render::Turn::default();
        loop {
            match self.rx.recv() {
                Ok(Signal::Event(event)) => match *event {
                    Ok(event) => {
                        for update in turn.event(&event) {
                            self.host.update(update);
                        }
                    }
                    Err(error) => return Err(format!("orchestrator: {error}")),
                },
                Ok(Signal::End) | Err(_) => break,
                Ok(Signal::Cancel) => {
                    let due = {
                        let mut cancel = lock(&cancel);
                        cancel.requested = true;
                        cancel.due()
                    };
                    if let Some(task) = due {
                        let client = client.clone();
                        thread::spawn(move || cancel_task(&client, &task));
                    }
                    return Ok(Ended::Cancelled);
                }
            }
        }
        if turn.stopped.is_none() {
            self.say("\nThe orchestrator closed the stream before the task stopped; `/status` shows where the run is.\n");
        }
        Ok(Ended::Done)
    }
}

fn lock<T>(mutex: &Mutex<T>) -> MutexGuard<'_, T> {
    mutex.lock().unwrap_or_else(|e| e.into_inner())
}

/// Cancelling a streamed task needs its id, which the stream may not have sent yet when the
/// editor cancels. Whichever side first knows both (the prompt thread, or the reader when the
/// task arrives) sends CancelTask, once.
#[derive(Default)]
struct Cancellation {
    requested: bool,
    task: Option<String>,
    sent: bool,
}

impl Cancellation {
    fn due(&mut self) -> Option<String> {
        if !self.requested || self.sent {
            return None;
        }
        let task = self.task.clone()?;
        self.sent = true;
        Some(task)
    }
}

fn cancel_task(client: &Client, task: &str) {
    if let Err(e) = client.cancel_task(task) {
        eprintln!("ln-agents: cancelling task {task}: {e}");
    }
}

/// The reader thread: forward every event; after a cancel, read on only until the task's id
/// is known, cancel it, and stop.
fn read_stream(
    client: &Client,
    request: &Request,
    tx: &Sender<Signal>,
    cancel: &Mutex<Cancellation>,
) {
    let events = match client.stream(request) {
        Ok(events) => events,
        Err(error) => {
            let _ = tx.send(Signal::Event(Box::new(Err(error))));
            return;
        }
    };
    for event in events {
        if let Ok(StreamResponse::Task(task)) = &event {
            let due = {
                let mut cancel = lock(cancel);
                cancel.task = Some(task.id.clone());
                cancel.due()
            };
            if let Some(task) = due {
                cancel_task(client, &task);
                return;
            }
        }
        let delivered = tx.send(Signal::Event(Box::new(event))).is_ok();
        let waiting = {
            let cancel = lock(cancel);
            cancel.requested && cancel.task.is_none()
        };
        if !delivered && !waiting {
            return; // the turn is over and there is nothing left to cancel
        }
    }
    let _ = tx.send(Signal::End);
}

/// The entry spec named by `/start`, relative to the session's cwd; must be a JSON object.
fn read_entry_spec(cwd: &Path, name: &str) -> Result<Value, String> {
    let path = cwd.join(name);
    let text = std::fs::read_to_string(&path).map_err(|e| format!("{}: {e}", path.display()))?;
    match serde_json::from_str(&text) {
        Ok(spec @ Value::Object(_)) => Ok(spec),
        Ok(_) => Err(format!(
            "{}: an entry spec is a JSON object",
            path.display()
        )),
        Err(e) => Err(format!("{}: {e}", path.display())),
    }
}
