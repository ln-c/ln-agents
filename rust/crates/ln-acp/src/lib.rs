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
pub mod turn;

use std::collections::HashMap;
use std::io::BufRead;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::mpsc::{Sender, channel};
use std::sync::{Arc, Mutex};
use std::thread;

use agent_client_protocol_schema::ProtocolVersion;
use agent_client_protocol_schema::v1::{
    AgentCapabilities, AvailableCommand, AvailableCommandInput, AvailableCommandsUpdate,
    CancelNotification, ContentBlock, Implementation, InitializeRequest, InitializeResponse,
    ListSessionsRequest, ListSessionsResponse, LoadSessionRequest, LoadSessionResponse,
    NewSessionRequest, NewSessionResponse, PermissionOption, PermissionOptionKind, PromptRequest,
    PromptResponse, RequestPermissionOutcome, RequestPermissionRequest, RequestPermissionResponse,
    SessionCapabilities, SessionInfo, SessionListCapabilities, SessionMode, SessionModeState,
    SessionNotification, SessionUpdate, SetSessionModeRequest, SetSessionModeResponse, ToolCall,
    ToolCallId, ToolCallStatus, ToolCallUpdate, ToolCallUpdateFields, ToolKind,
    UnstructuredCommandInput,
};
use commands::Mode;
use ln_a2a::{Client, Endpoint, Request};
use rpc::{Connection, INVALID_PARAMS, Incoming, METHOD_NOT_FOUND};
use serde::de::DeserializeOwned;
use serde_json::Value;
use turn::{Host, Prompt, Signal, latest_per_run, skill_reply};

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
            let host = AcpHost {
                agent: agent.clone(),
                session_id: session_id.clone(),
                cwd: cwd.clone(),
            };
            let turn = Prompt::with_channel(host, cwd, run, tx, rx);
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

/// An editor's session as the place a turn is shown: updates go out as `session/update`, and
/// `/decide` and `/resume` open a terminal window.
struct AcpHost {
    agent: Agent,
    session_id: String,
    cwd: PathBuf,
}

impl AcpHost {
    fn with_session<T>(&self, f: impl FnOnce(&mut Session) -> T) -> Option<T> {
        let mut sessions = self
            .agent
            .sessions
            .lock()
            .unwrap_or_else(|e| e.into_inner());
        sessions.get_mut(&self.session_id).map(f)
    }
}

impl Host for AcpHost {
    fn update(&self, update: SessionUpdate) {
        self.agent.update(&self.session_id, update);
    }

    fn client(&self) -> Result<Client, String> {
        self.agent.config.client(&self.cwd)
    }

    fn setup(&self) -> &human::Setup {
        &self.agent.config.human
    }

    fn bind(&self, run: &str) {
        self.with_session(|session| session.run = Some(run.to_string()));
    }

    fn remember(&self, pending: human::Pending) {
        self.with_session(|session| session.pending = Some(pending));
    }

    fn pending(&self) -> Option<human::Pending> {
        self.with_session(|session| session.pending.clone())
            .flatten()
    }

    /// Ask before writing the document: allow or reject this once, never "always".
    fn allow_write(&self, run: &str, decision: &str) -> bool {
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
        allowed
    }

    /// Open the human's terminal window and say what to do in it, or how to do it by hand.
    fn hand_over(&self, args: &[String], run: &str, what: &str, manual: &str) {
        let text = match self.agent.config.window.open(&self.cwd, args) {
            Ok(command) => render::window_opened(run, what, &command),
            Err(error) => render::window_failed(&error, manual),
        };
        self.agent.say(&self.session_id, text);
    }
}
