//! A2A client: `ln-agents` talks to the Python orchestrator (`a2a_server.py`, agent `ln-pipeline`)
//! over JSON-RPC and SSE on loopback.
//!
//! The client can express exactly the server's six skills ([`Request`]); none of them signs,
//! decides or resumes a run. Its rules, from the server's behaviour:
//!
//! * `status`, `evidence` and `approval_document` are queries: they go without a task and name
//!   the run, and the server answers them with a message;
//! * `start_run` and `advance` each open a new task, streamed until the server ends it;
//! * the bearer token goes only to a literal loopback address, never through a proxy.

use std::fmt;
use std::io::{BufRead, BufReader, Read};
use std::net::IpAddr;
use std::path::Path;
use std::time::Duration;

pub use a2a as types;
use a2a::{
    AgentCard, JsonRpcError, Message, Part, PartContent, Role, SendMessageResponse, StreamResponse,
    Task,
};
use serde_json::{Map, Value, json};

/// The orchestrator's agent card name.
pub const AGENT_NAME: &str = "ln-pipeline";
/// The server's default endpoint (`python -m agentic_pipeline serve a2a`).
pub const DEFAULT_URL: &str = "http://127.0.0.1:8420/a2a/jsonrpc";
/// The task metadata field naming the run a task advances.
pub const RUN_ID_KEY: &str = "run_id";
const CARD_PATH: &str = "/.well-known/agent-card.json";
const MIN_TOKEN_LEN: usize = 32;
const PAGE_SIZE: u32 = 100;
const MAX_PAGES: usize = 1000;

#[derive(Debug)]
pub enum Error {
    /// Unusable configuration: a non-loopback URL, an unreadable or exposed token file.
    Config(String),
    /// The server refused the request (a JSON-RPC error).
    Refused { code: i32, message: String },
    /// An HTTP status other than 200 (401 is a wrong or missing token).
    Http { status: u16, body: String },
    /// The server could not be reached, or the connection broke.
    Transport(String),
    /// A reply that is not what the protocol promises.
    Protocol(String),
}

impl fmt::Display for Error {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Error::Config(m) => write!(f, "configuration: {m}"),
            Error::Refused { code, message } => write!(f, "refused ({code}): {message}"),
            Error::Http { status, body } => write!(f, "HTTP {status}: {body}"),
            Error::Transport(m) => write!(f, "cannot reach the orchestrator: {m}"),
            Error::Protocol(m) => write!(f, "unexpected reply: {m}"),
        }
    }
}

impl std::error::Error for Error {}

pub type Result<T> = std::result::Result<T, Error>;

/// One skill call. Only these seven exist on the server; there is no way to build another.
#[derive(Debug, Clone, PartialEq)]
pub struct Request {
    skill: &'static str,
    args: Map<String, Value>,
}

impl Request {
    fn new(skill: &'static str, args: Value) -> Self {
        let Value::Object(args) = args else {
            unreachable!("skill arguments are built as objects")
        };
        Request { skill, args }
    }

    pub fn start_run(run_id: &str, entry_spec: Value) -> Self {
        Self::new(
            "start_run",
            json!({"run_id": run_id, "entry_spec": entry_spec}),
        )
    }

    pub fn advance(run_id: &str) -> Self {
        Self::new("advance", json!({"run_id": run_id}))
    }

    pub fn status(run_id: &str) -> Self {
        Self::new("status", json!({"run_id": run_id}))
    }

    pub fn evidence(run_id: &str) -> Self {
        Self::new("evidence", json!({"run_id": run_id}))
    }

    /// Writes the unsigned document; the human signs it and runs `run decide`.
    /// `candidate` names the surviving design an approval at H1 chooses; nothing else has one.
    pub fn approval_document(
        run_id: &str,
        decision: &str,
        approver: &str,
        reason: &str,
        candidate: Option<&str>,
    ) -> Self {
        let mut args =
            json!({"run_id": run_id, "decision": decision, "approver": approver, "reason": reason});
        if let Some(candidate) = candidate {
            args["candidate"] = Value::from(candidate);
        }
        Self::new("approval_document", args)
    }

    /// A question for the coordinator model about a run (the task's own when `run_id` is None).
    /// It answers in text from the run's views and changes nothing.
    pub fn ask(run_id: Option<&str>, question: &str) -> Self {
        let mut args = json!({"question": question});
        if let Some(run_id) = run_id {
            args["run_id"] = Value::from(run_id);
        }
        Self::new("ask", args)
    }

    /// The model catalog of the repository's registry. It is about the repository, not a run,
    /// so it takes no run id; read-only, and no model is called.
    pub fn models() -> Self {
        Self::new("models", json!({}))
    }

    pub fn skill(&self) -> &'static str {
        self.skill
    }

    /// The A2A message: one data part `{"skill": ..., ...args}`, bound to no task.
    pub fn message(&self) -> Message {
        let mut data = self.args.clone();
        data.insert("skill".into(), Value::from(self.skill));
        Message::new(Role::User, vec![Part::data(Value::Object(data))])
    }
}

/// Where the server is and the bearer token for it.
#[derive(Clone)]
pub struct Endpoint {
    url: String,
    origin: String,
    token: String,
}

impl fmt::Debug for Endpoint {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.debug_struct("Endpoint")
            .field("url", &self.url)
            .finish_non_exhaustive()
    }
}

impl Endpoint {
    /// `url` must be `http://<loopback IP>:<port>/...`: the token never leaves the host.
    pub fn new(url: &str, token: String) -> Result<Self> {
        let origin = loopback_origin(url)?;
        if token.len() < MIN_TOKEN_LEN {
            return Err(Error::Config(format!(
                "the token must be at least {MIN_TOKEN_LEN} characters"
            )));
        }
        Ok(Endpoint {
            url: url.to_string(),
            origin,
            token,
        })
    }

    /// The token from the server's token file (`<runs>/.a2a/token`), refused if others may
    /// read it.
    pub fn from_token_file(url: &str, path: &Path) -> Result<Self> {
        let token = read_token(path)?;
        Self::new(url, token)
    }

    pub fn url(&self) -> &str {
        &self.url
    }
}

/// `http://<host>:<port>` for a URL whose host is a literal loopback IP (no names: a name could
/// resolve elsewhere).
pub fn loopback_origin(url: &str) -> Result<String> {
    let rest = url
        .strip_prefix("http://")
        .ok_or_else(|| Error::Config(format!("{url}: only http:// to a loopback address")))?;
    let authority = rest.split('/').next().unwrap_or_default();
    let (host, port) = authority
        .rsplit_once(':')
        .ok_or_else(|| Error::Config(format!("{url}: no port")))?;
    port.parse::<u16>()
        .map_err(|_| Error::Config(format!("{url}: bad port {port:?}")))?;
    let bare = host
        .strip_prefix('[')
        .and_then(|h| h.strip_suffix(']'))
        .unwrap_or(host);
    let ip: IpAddr = bare
        .parse()
        .map_err(|_| Error::Config(format!("{url}: {host} is not a literal IP address")))?;
    if !ip.is_loopback() {
        return Err(Error::Config(format!(
            "{url}: {host} is not a loopback address"
        )));
    }
    Ok(format!("http://{authority}"))
}

fn read_token(path: &Path) -> Result<String> {
    let text = std::fs::read_to_string(path)
        .map_err(|e| Error::Config(format!("token file {}: {e}", path.display())))?;
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        let mode = std::fs::metadata(path)
            .map_err(|e| Error::Config(format!("token file {}: {e}", path.display())))?
            .permissions()
            .mode();
        if mode & 0o077 != 0 {
            return Err(Error::Config(format!(
                "token file {} is readable by others; chmod 600 it",
                path.display()
            )));
        }
    }
    Ok(text.trim().to_string())
}

/// A blocking client; each call is one HTTP request.
#[derive(Debug, Clone)]
pub struct Client {
    endpoint: Endpoint,
    agent: ureq::Agent,
}

impl Client {
    pub fn new(endpoint: Endpoint) -> Self {
        let config = ureq::Agent::config_builder()
            .proxy(None) // never route the token through an HTTP(S)_PROXY from the environment
            .max_redirects(0)
            .http_status_as_error(false)
            .timeout_connect(Some(Duration::from_secs(5)))
            .build();
        Client {
            endpoint,
            agent: config.into(),
        }
    }

    pub fn endpoint(&self) -> &Endpoint {
        &self.endpoint
    }

    /// The public agent card; refused unless it is `ln-pipeline`'s.
    pub fn card(&self) -> Result<AgentCard> {
        let url = format!("{}{CARD_PATH}", self.endpoint.origin);
        let response = self.agent.get(&url).call().map_err(transport)?;
        let card: AgentCard = serde_json::from_str(&read_body(response)?)
            .map_err(|e| Error::Protocol(format!("agent card: {e}")))?;
        if card.name != AGENT_NAME {
            return Err(Error::Protocol(format!(
                "agent {:?} is not {AGENT_NAME}",
                card.name
            )));
        }
        Ok(card)
    }

    /// A task-less skill call (`status`, `evidence`, `approval_document`).
    pub fn send(&self, request: &Request) -> Result<SendMessageResponse> {
        decode(self.rpc("SendMessage", json!({"message": request.message()}))?)
    }

    /// `start_run` or `advance`: a new task, streamed until the server ends it.
    pub fn stream(&self, request: &Request) -> Result<Events> {
        let body = envelope(
            "SendStreamingMessage",
            json!({"message": request.message()}),
        );
        let response = self.post(body)?;
        Ok(Events {
            lines: BufReader::new(Box::new(response.into_body().into_reader())),
        })
    }

    pub fn get_task(&self, id: &str) -> Result<Task> {
        decode(self.rpc("GetTask", json!({"id": id}))?)
    }

    /// Every stored task, without history, following the server's pages to the end.
    pub fn list_tasks(&self) -> Result<Vec<Task>> {
        let mut tasks: Vec<Task> = Vec::new();
        let mut token = String::new();
        for _ in 0..MAX_PAGES {
            let mut params = json!({"historyLength": 0, "pageSize": PAGE_SIZE});
            if !token.is_empty() {
                params["pageToken"] = Value::from(token.clone());
            }
            let mut page = self.rpc("ListTasks", params)?;
            match page.get_mut("tasks").map(Value::take) {
                None | Some(Value::Null) => {} // an empty page omits the field
                Some(found) => tasks.extend(decode::<Vec<Task>>(found)?),
            }
            let next = page
                .get("nextPageToken")
                .and_then(Value::as_str)
                .unwrap_or_default();
            let next = next.to_string();
            if next.is_empty() {
                return Ok(tasks);
            }
            if next == token {
                return Err(Error::Protocol("ListTasks repeated its page token".into()));
            }
            token = next;
        }
        Err(Error::Protocol(format!(
            "ListTasks did not end within {MAX_PAGES} pages"
        )))
    }

    /// Stops the run after its current step; the run stays resumable.
    pub fn cancel_task(&self, id: &str) -> Result<Task> {
        decode(self.rpc("CancelTask", json!({"id": id}))?)
    }

    fn post(&self, body: Value) -> Result<ureq::http::Response<ureq::Body>> {
        let response = self
            .agent
            .post(&self.endpoint.url)
            .header("Authorization", &format!("Bearer {}", self.endpoint.token))
            .header(a2a::SVC_PARAM_VERSION, a2a::VERSION)
            .send_json(body)
            .map_err(transport)?;
        let status = response.status().as_u16();
        if status != 200 {
            let body = read_body(response).unwrap_or_default();
            return Err(Error::Http {
                status,
                body: body.chars().take(200).collect(),
            });
        }
        Ok(response)
    }

    fn rpc(&self, method: &str, params: Value) -> Result<Value> {
        let response = self.post(envelope(method, params))?;
        let reply: Value = serde_json::from_str(&read_body(response)?)
            .map_err(|e| Error::Protocol(format!("{method}: {e}")))?;
        result_of(reply)
    }
}

fn envelope(method: &str, params: Value) -> Value {
    json!({"jsonrpc": "2.0", "id": 1, "method": method, "params": params})
}

fn transport(error: ureq::Error) -> Error {
    Error::Transport(error.to_string())
}

fn read_body(response: ureq::http::Response<ureq::Body>) -> Result<String> {
    response.into_body().read_to_string().map_err(transport)
}

fn decode<T: serde::de::DeserializeOwned>(value: Value) -> Result<T> {
    serde_json::from_value(value).map_err(|e| Error::Protocol(e.to_string()))
}

/// The `result` of a JSON-RPC reply, or its error as [`Error::Refused`].
fn result_of(mut reply: Value) -> Result<Value> {
    if let Some(error) = reply.get("error") {
        let error: JsonRpcError = decode(error.clone())?;
        return Err(Error::Refused {
            code: error.code,
            message: error.message,
        });
    }
    reply
        .get_mut("result")
        .map(Value::take)
        .ok_or_else(|| Error::Protocol("a JSON-RPC reply with neither result nor error".into()))
}

/// The events of one streamed task, read as they arrive (Server-Sent Events).
pub struct Events {
    lines: BufReader<Box<dyn Read + Send>>,
}

impl Events {
    /// Events from any reader of an SSE body (used by tests with recorded streams).
    pub fn from_reader(reader: impl Read + Send + 'static) -> Self {
        Events {
            lines: BufReader::new(Box::new(reader)),
        }
    }
}

impl Iterator for Events {
    type Item = Result<StreamResponse>;

    fn next(&mut self) -> Option<Self::Item> {
        let mut data = String::new();
        loop {
            let mut line = String::new();
            match self.lines.read_line(&mut line) {
                Ok(0) if data.is_empty() => return None,
                Ok(0) => break,
                Ok(_) => {}
                Err(e) => return Some(Err(Error::Transport(e.to_string()))),
            }
            let line = line.trim_end_matches(['\r', '\n']);
            if line.is_empty() {
                if data.is_empty() {
                    continue;
                }
                break;
            }
            if let Some(payload) = line.strip_prefix("data:") {
                if !data.is_empty() {
                    data.push('\n');
                }
                data.push_str(payload.strip_prefix(' ').unwrap_or(payload));
            }
        }
        let parsed = serde_json::from_str::<Value>(&data)
            .map_err(|e| Error::Protocol(format!("stream event: {e}")))
            .and_then(result_of)
            .and_then(decode);
        Some(parsed)
    }
}

/// The run a task advances, from its metadata.
pub fn run_id(task: &Task) -> Option<&str> {
    task.metadata.as_ref()?.get(RUN_ID_KEY)?.as_str()
}

/// The text parts of a message, joined by newlines.
pub fn text(message: &Message) -> String {
    let texts: Vec<&str> = message.parts.iter().filter_map(Part::as_text).collect();
    texts.join("\n")
}

/// The first data part of a message, if any.
pub fn data(parts: &[Part]) -> Option<&Value> {
    parts.iter().find_map(|p| match &p.content {
        PartContent::Data(v) => Some(v),
        _ => None,
    })
}
