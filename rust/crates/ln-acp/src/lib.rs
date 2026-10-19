//! ACP (Agent Client Protocol) agent side of `ln-agents acp`: editors such as Zed and JetBrains
//! start it and talk JSON-RPC over stdio. Every prompt is a slash command run as code over one
//! A2A skill of `ln-pipeline` (see [`commands`]); nothing here approves, decides or resumes a
//! run by itself, and editor-supplied MCP servers are accepted and ignored (pipeline tools are
//! policy). `/decide` and `/resume` only open a terminal window where the human does it (see
//! [`human`]).
//!
//! An ACP session is a conversation about runs: `/start` or `/advance` binds it to a run, and
//! `session/list` lists the runs the orchestrator knows (as `run/<id>`, loadable).

pub mod commands;
pub mod human;
pub mod render;
pub mod rpc;

use std::collections::HashMap;
use std::io::BufRead;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::mpsc::{Receiver, Sender, channel};
use std::sync::{Arc, Mutex, MutexGuard};
use std::thread;

use agent_client_protocol_schema::ProtocolVersion;
use agent_client_protocol_schema::v1::{
    AgentCapabilities, AvailableCommand, AvailableCommandInput, AvailableCommandsUpdate,
    CancelNotification, ContentBlock, Implementation, InitializeRequest, InitializeResponse,
    ListSessionsRequest, ListSessionsResponse, LoadSessionRequest, LoadSessionResponse,
    NewSessionRequest, NewSessionResponse, PermissionOption, PermissionOptionKind, PromptRequest,
    PromptResponse, RequestPermissionOutcome, RequestPermissionRequest, RequestPermissionResponse,
    SessionCapabilities, SessionInfo, SessionListCapabilities, SessionMode, SessionModeState,
    SessionNotification, SessionUpdate, SetSessionModeRequest, SetSessionModeResponse, StopReason,
    ToolCall, ToolCallId, ToolCallStatus, ToolCallUpdate, ToolCallUpdateFields, ToolKind,
    UnstructuredCommandInput,
};
use commands::{Command, Mode};
use ln_a2a::types::{SendMessageResponse, StreamResponse, Task};
use ln_a2a::{Client, Endpoint, Request};
use rpc::{Connection, INVALID_PARAMS, Incoming, METHOD_NOT_FOUND};
use serde::de::DeserializeOwned;
use serde_json::Value;

/// A session id that names a run, as `session/list` reports it and `session/load` accepts it.
pub const RUN_SESSION_PREFIX: &str = "run/";
/// The server's token file relative to a session's cwd when none is configured.
pub const DEFAULT_TOKEN_FILE: &str = ".runs/.a2a/token";
const WRITE_OPTION: &str = "write";
const SKIP_OPTION: &str = "skip";

#[derive(Debug, Clone)]
pub struct Config {
    /// The orchestrator's A2A endpoint (a literal loopback address).
    pub url: String,
    /// The bearer token file; `<session cwd>/.runs/.a2a/token` when unset.
    pub token_file: Option<PathBuf>,
    /// What `/decide` and `/resume` put in their terminal windows.
    pub human: human::Setup,
    /// Opens those windows (the binary implements it; this crate starts no process).
    pub window: human::SharedWindow,
}

impl Config {
    fn client(&self, cwd: &Path) -> Result<Client, String> {
        let path = self
            .token_file
            .clone()
            .unwrap_or_else(|| cwd.join(DEFAULT_TOKEN_FILE));
        let endpoint = Endpoint::from_token_file(&self.url, &path).map_err(|e| {
            format!("{e}. Start the orchestrator with `python -m agentic_pipeline serve a2a`, or set --token-file / LN_AGENTS_TOKEN_FILE.")
        })?;
        Ok(Client::new(endpoint))
    }
}

enum Signal {
    Event(Box<ln_a2a::Result<StreamResponse>>),
    End,
    Cancel,
}

struct Session {
    cwd: PathBuf,
    mode: Mode,
    run: Option<String>,
    /// The unsigned document `/approval-doc` last wrote in this session, for `/decide`.
    pending: Option<human::Pending>,
    turn: Option<Sender<Signal>>,
}

#[derive(Clone)]
struct Agent {
    conn: Connection,
    config: Arc<Config>,
    sessions: Arc<Mutex<HashMap<String, Session>>>,
    next_id: Arc<AtomicU64>, // numbers session and tool call ids
}

/// Serve one editor connection until its input closes.
pub fn serve(input: impl BufRead, output: impl std::io::Write + Send + 'static, config: Config) {
    let agent = Agent {
        conn: Connection::new(output),
        config: Arc::new(config),
        sessions: Arc::default(),
        next_id: Arc::default(),
    };
    for line in input.lines() {
        let Ok(line) = line else { break };
        if line.trim().is_empty() {
            continue;
        }
        match rpc::parse(&line) {
            Ok(Incoming::Request { id, method, params }) => agent.request(id, &method, params),
            Ok(Incoming::Notification { method, params }) => agent.notification(&method, params),
            Ok(Incoming::Response { id, outcome }) => agent.conn.resolve(&id, outcome),
            Err((code, message)) => agent.conn.respond_error(&Value::Null, code, &message),
        }
    }
    agent.conn.close();
}

fn decode<T: DeserializeOwned>(params: Value) -> Result<T, String> {
    serde_json::from_value(params).map_err(|e| format!("invalid params: {e}"))
}

fn mode_state(mode: Mode) -> SessionModeState {
    let modes = Mode::ALL
        .iter()
        .map(|m| SessionMode::new(m.id(), m.name()).description(m.description().to_string()))
        .collect();
    SessionModeState::new(mode.id(), modes)
}

impl Agent {
    fn request(&self, id: Value, method: &str, params: Value) {
        let outcome = match method {
            "initialize" => self.initialize(params),
            "session/new" => self.new_session(&id, params),
            "session/load" => self.load_session(&id, params),
            "session/list" => self.list_sessions(params),
            "session/set_mode" => self.set_mode(&id, params),
            "session/prompt" => self.prompt(&id, params),
            _ => {
                self.conn.respond_error(
                    &id,
                    METHOD_NOT_FOUND,
                    &format!("{method} is not supported"),
                );
                return;
            }
        };
        match outcome {
            Ok(Some(result)) => self.conn.respond(&id, result),
            Ok(None) => {} // answered already, or answered later by a prompt thread
            Err(message) => self.conn.respond_error(&id, INVALID_PARAMS, &message),
        }
    }

    fn notification(&self, method: &str, params: Value) {
        if method != "session/cancel" {
            return; // unknown notifications are ignored, as JSON-RPC allows
        }
        let Ok(cancel) = decode::<CancelNotification>(params) else {
            return;
        };
        let sessions = self.sessions.lock().unwrap_or_else(|e| e.into_inner());
        if let Some(turn) = sessions
            .get(cancel.session_id.0.as_ref())
            .and_then(|s| s.turn.as_ref())
        {
            let _ = turn.send(Signal::Cancel);
        }
    }

    fn update(&self, session_id: &str, update: SessionUpdate) {
        self.conn.notify(
            "session/update",
            SessionNotification::new(session_id.to_string(), update),
        );
    }

    fn say(&self, session_id: &str, text: impl Into<String>) {
        self.update(session_id, render::message(text));
    }

    fn announce_commands(&self, session_id: &str, mode: Mode) {
        let commands = commands::available(mode)
            .into_iter()
            .map(|(name, hint, description)| {
                let command = AvailableCommand::new(name, description);
                if hint.is_empty() {
                    command
                } else {
                    command.input(AvailableCommandInput::Unstructured(
                        UnstructuredCommandInput::new(hint),
                    ))
                }
            })
            .collect();
        self.update(
            session_id,
            SessionUpdate::AvailableCommandsUpdate(AvailableCommandsUpdate::new(commands)),
        );
    }

    fn initialize(&self, params: Value) -> Result<Option<Value>, String> {
        decode::<InitializeRequest>(params)?;
        let capabilities = AgentCapabilities::new()
            .load_session(true)
            .session_capabilities(SessionCapabilities::new().list(SessionListCapabilities::new()));
        let response = InitializeResponse::new(ProtocolVersion::V1)
            .agent_capabilities(capabilities)
            .agent_info(Implementation::new("ln-agents", env!("CARGO_PKG_VERSION")));
        Ok(Some(to_value(response)))
    }

    fn insert_session(&self, id: &str, cwd: PathBuf, run: Option<String>) {
        let session = Session {
            cwd,
            mode: Mode::Status,
            run,
            pending: None,
            turn: None,
        };
        self.sessions
            .lock()
            .unwrap_or_else(|e| e.into_inner())
            .insert(id.to_string(), session);
    }

    fn new_session(&self, id: &Value, params: Value) -> Result<Option<Value>, String> {
        let request: NewSessionRequest = decode(params)?; // its mcpServers are not used
        let n = self.next_id.fetch_add(1, Ordering::Relaxed);
        let session_id = format!("ln-{}-{n}", std::process::id());
        self.insert_session(&session_id, request.cwd, None);
        let response = NewSessionResponse::new(session_id.clone()).modes(mode_state(Mode::Status));
        self.conn.respond(id, response);
        self.announce_commands(&session_id, Mode::Status);
        Ok(None)
    }

    /// `run/<id>`: a session bound to that run, replayed as its current status.
    fn load_session(&self, id: &Value, params: Value) -> Result<Option<Value>, String> {
        let request: LoadSessionRequest = decode(params)?;
        let session_id = request.session_id.0.to_string();
        let run = session_id.strip_prefix(RUN_SESSION_PREFIX).ok_or_else(|| {
            format!("{session_id} is not a run session ({RUN_SESSION_PREFIX}<id>)")
        })?;
        let client = self.config.client(&request.cwd)?;
        let status = skill_reply(&client, &Request::status(run)).map_err(|e| e.to_string())?;
        self.insert_session(&session_id, request.cwd, Some(run.to_string()));
        self.say(&session_id, render::status_text(&status));
        self.conn.respond(
            id,
            LoadSessionResponse::new().modes(mode_state(Mode::Status)),
        );
        self.announce_commands(&session_id, Mode::Status);
        Ok(None)
    }

    fn list_sessions(&self, params: Value) -> Result<Option<Value>, String> {
        let request: ListSessionsRequest = decode(params)?;
        let cwd = match request.cwd {
            Some(cwd) => cwd,
            None => std::env::current_dir().map_err(|e| e.to_string())?,
        };
        let tasks = self
            .config
            .client(&cwd)?
            .list_tasks()
            .map_err(|e| e.to_string())?;
        let sessions = latest_per_run(tasks)
            .into_iter()
            .map(|(run, task)| {
                let state = format!("{:?}", task.status.state);
                SessionInfo::new(format!("{RUN_SESSION_PREFIX}{run}"), cwd.clone())
                    .title(format!("{run} ({state})"))
                    .updated_at(task.status.timestamp.map(|t| t.to_rfc3339()))
            })
            .collect();
        Ok(Some(to_value(ListSessionsResponse::new(sessions))))
    }

    fn set_mode(&self, id: &Value, params: Value) -> Result<Option<Value>, String> {
        let request: SetSessionModeRequest = decode(params)?;
        let mode =
            Mode::from_id(&request.mode_id.0).ok_or("unknown mode; modes: status, design")?;
        let session_id = request.session_id.0.to_string();
        {
            let mut sessions = self.sessions.lock().unwrap_or_else(|e| e.into_inner());
            let session = sessions.get_mut(&session_id).ok_or("unknown session")?;
            session.mode = mode;
        }
        self.conn.respond(id, SetSessionModeResponse::new());
        self.announce_commands(&session_id, mode);
        Ok(None)
    }

    fn prompt(&self, id: &Value, params: Value) -> Result<Option<Value>, String> {
        let request: PromptRequest = decode(params)?;
        let session_id = request.session_id.0.to_string();
        let text: Vec<String> = request
            .prompt
            .iter()
            .filter_map(|block| match block {
                ContentBlock::Text(t) => Some(t.text.clone()),
                _ => None,
            })
            .collect();
        let (tx, rx) = channel();
        let (mode, cwd, run) = {
            let mut sessions = self.sessions.lock().unwrap_or_else(|e| e.into_inner());
            let session = sessions.get_mut(&session_id).ok_or("unknown session")?;
            if session.turn.is_some() {
                return Err("a prompt is already running in this session".into());
            }
            session.turn = Some(tx.clone());
            (session.mode, session.cwd.clone(), session.run.clone())
        };
        let agent = self.clone();
        let id = id.clone();
        thread::spawn(move || {
            let turn = Prompt {
                agent: agent.clone(),
                session_id: session_id.clone(),
                cwd,
                run,
                tx,
                rx,
            };
            let stop = turn.run(&text.join("\n"), mode);
            if let Some(session) = agent
                .sessions
                .lock()
                .unwrap_or_else(|e| e.into_inner())
                .get_mut(&session_id)
            {
                session.turn = None;
            }
            agent.conn.respond(&id, PromptResponse::new(stop));
        });
        Ok(None)
    }
}

fn to_value(value: impl serde::Serialize) -> Value {
    serde_json::to_value(value).expect("ACP types serialize")
}

/// The data part of a task-less skill reply.
fn skill_reply(client: &Client, request: &Request) -> ln_a2a::Result<Value> {
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

/// One task per run: the one whose status changed last.
fn latest_per_run(tasks: Vec<Task>) -> Vec<(String, Task)> {
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
struct Prompt {
    agent: Agent,
    session_id: String,
    cwd: PathBuf,
    run: Option<String>,
    tx: Sender<Signal>,
    rx: Receiver<Signal>,
}

enum Ended {
    Done,
    Cancelled,
}

impl Prompt {
    fn say(&self, text: impl Into<String>) {
        self.agent.say(&self.session_id, text);
    }

    fn cancelled(&self) -> bool {
        matches!(self.rx.try_recv(), Ok(Signal::Cancel))
    }

    fn run(&self, text: &str, mode: Mode) -> StopReason {
        let ended = match commands::parse(text, mode) {
            Err(refusal) => {
                self.say(format!("{refusal}\n"));
                Ok(Ended::Done)
            }
            Ok(command) => self.execute(command, mode),
        };
        match ended {
            Ok(Ended::Done) if !self.cancelled() => StopReason::EndTurn,
            Ok(_) => StopReason::Cancelled,
            Err(error) => {
                self.say(format!("\n{error}\n"));
                if self.cancelled() {
                    StopReason::Cancelled
                } else {
                    StopReason::EndTurn
                }
            }
        }
    }

    fn the_run(&self, given: Option<String>) -> Result<String, String> {
        given.or_else(|| self.run.clone()).ok_or_else(|| {
            "No run in this session: name one (`/status <run>`), pick one from `/runs`, or \
             `/start` one."
                .to_string()
        })
    }

    fn bind(&self, run: &str) {
        let mut sessions = self
            .agent
            .sessions
            .lock()
            .unwrap_or_else(|e| e.into_inner());
        if let Some(session) = sessions.get_mut(&self.session_id) {
            session.run = Some(run.to_string());
        }
    }

    fn execute(&self, command: Command, mode: Mode) -> Result<Ended, String> {
        let client = || self.agent.config.client(&self.cwd);
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
                    self.agent.update(&self.session_id, update);
                }
            }
            Command::Models => {
                let catalog = skill_reply(&client()?, &Request::models()).map_err(a2a)?;
                self.say(render::models_text(&catalog));
            }
            Command::Ask { question } => {
                let run = self.the_run(None)?;
                let request = Request::ask(Some(&run), &question);
                let reply = skill_reply(&client()?, &request).map_err(a2a)?;
                self.say(render::answer_text(&reply));
            }
            Command::Start { run, entry_spec } => {
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
                if !self.permission(&run, &decision)? {
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
                let setup = &self.agent.config.human;
                let args = human::decide_args(setup, &run, &pending.document, &pending.namespace)?;
                let manual = format!("{}\n{}", pending.sign, pending.then);
                self.open_window(&args, &run, "sign and record your decision", &manual);
            }
            Command::Resume { by, reason } => {
                let run = self.the_run(None)?;
                let setup = &self.agent.config.human;
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
        let mut sessions = self
            .agent
            .sessions
            .lock()
            .unwrap_or_else(|e| e.into_inner());
        if let Some(session) = sessions.get_mut(&self.session_id) {
            session.pending = Some(pending);
        }
    }

    fn pending_for(&self, run: &str) -> Result<human::Pending, String> {
        let sessions = self
            .agent
            .sessions
            .lock()
            .unwrap_or_else(|e| e.into_inner());
        sessions
            .get(&self.session_id)
            .and_then(|session| session.pending.clone())
            .filter(|pending| pending.run == run)
            .ok_or_else(|| {
                format!(
                    "No unsigned document for run `{run}` in this session: write one with \
                     `/approval-doc <decision> <approver> <reason>` first."
                )
            })
    }

    /// Open the human's terminal window and say what to do in it, or how to do it by hand.
    fn open_window(&self, args: &[String], run: &str, what: &str, manual: &str) {
        match self.agent.config.window.open(&self.cwd, args) {
            Ok(command) => self.say(render::window_opened(run, what, &command)),
            Err(error) => self.say(render::window_failed(&error, manual)),
        }
    }

    /// Ask before writing the document: allow or reject this once, never "always".
    fn permission(&self, run: &str, decision: &str) -> Result<bool, String> {
        let n = self.agent.next_id.fetch_add(1, Ordering::Relaxed);
        let call_id = format!("approval-doc-{n}");
        let title = format!(
            "Write an unsigned {decision} document for run {run} (it approves nothing until a \
             human signs it and runs `run decide`)"
        );
        let update = |status| {
            SessionUpdate::ToolCallUpdate(ToolCallUpdate::new(
                ToolCallId::new(call_id.as_str()),
                ToolCallUpdateFields::new().status(status),
            ))
        };
        self.agent.update(
            &self.session_id,
            SessionUpdate::ToolCall(
                ToolCall::new(ToolCallId::new(call_id.as_str()), title.as_str())
                    .kind(ToolKind::Edit)
                    .status(ToolCallStatus::Pending),
            ),
        );
        let options = vec![
            PermissionOption::new(
                WRITE_OPTION,
                "Write the document",
                PermissionOptionKind::AllowOnce,
            ),
            PermissionOption::new(
                SKIP_OPTION,
                "Don't write it",
                PermissionOptionKind::RejectOnce,
            ),
        ];
        let ask = RequestPermissionRequest::new(
            self.session_id.clone(),
            ToolCallUpdate::new(
                ToolCallId::new(call_id.as_str()),
                ToolCallUpdateFields::new()
                    .title(title.clone())
                    .kind(ToolKind::Edit),
            ),
            options,
        );
        let answer = self.agent.conn.request("session/request_permission", ask);
        let allowed = match answer.recv() {
            Ok(Ok(value)) => matches!(
                serde_json::from_value::<RequestPermissionResponse>(value).map(|r| r.outcome),
                Ok(RequestPermissionOutcome::Selected(choice)) if choice.option_id.0.as_ref() == WRITE_OPTION
            ),
            _ => false,
        };
        let status = if allowed {
            ToolCallStatus::Completed
        } else {
            ToolCallStatus::Failed
        };
        self.agent.update(&self.session_id, update(status));
        Ok(allowed)
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
                            self.agent.update(&self.session_id, update);
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
