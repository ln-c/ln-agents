//! `ln-hub acp` as an editor sees it: the binary on stdio, against a fake `ln-pipeline` that
//! replays what the real A2A server sent (ln-a2a/tests/fixtures).

#[path = "../../ln-a2a/tests/fake/mod.rs"]
mod fake;

use std::io::{BufRead, BufReader, Write};
use std::path::Path;
use std::process::{Child, ChildStdin, Command, Stdio};
use std::sync::mpsc::{Receiver, channel};
use std::thread;
use std::time::{Duration, Instant};

use fake::{Fake, Reply, fixture, scratch, token_file};
use serde_json::{Value, json};

const WAIT: Duration = Duration::from_secs(20);

struct Editor {
    child: Child,
    stdin: ChildStdin,
    lines: Receiver<Value>,
    next: i64,
}

impl Editor {
    fn start(url: &str, token: &Path) -> Self {
        Editor::start_with(url, token, &[])
    }

    fn start_with(url: &str, token: &Path, env: &[(&str, &str)]) -> Self {
        let mut child = Command::new(env!("CARGO_BIN_EXE_ln-hub"))
            .args(["acp", "--a2a-url", url, "--token-file"])
            .arg(token)
            .env_remove("LN_HUB_A2A_URL")
            .env_remove("LN_HUB_TOKEN_FILE")
            .envs(env.iter().copied())
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .spawn()
            .expect("ln-hub starts");
        let stdin = child.stdin.take().unwrap();
        let stdout = BufReader::new(child.stdout.take().unwrap());
        let (tx, lines) = channel();
        thread::spawn(move || {
            for line in stdout.lines() {
                let line = line.expect("stdout");
                let value = serde_json::from_str(&line).unwrap_or_else(|e| {
                    panic!("stdout carries only JSON-RPC lines: {line:?}: {e}")
                });
                if tx.send(value).is_err() {
                    return;
                }
            }
        });
        Editor {
            child,
            stdin,
            lines,
            next: 100,
        }
    }

    fn write(&mut self, message: Value) {
        writeln!(self.stdin, "{message}").unwrap();
        self.stdin.flush().unwrap();
    }

    fn send(&mut self, method: &str, params: Value) -> i64 {
        self.next += 1;
        let id = self.next;
        self.write(json!({"jsonrpc": "2.0", "id": id, "method": method, "params": params}));
        id
    }

    fn next_message(&self) -> Value {
        self.lines
            .recv_timeout(WAIT)
            .expect("ln-hub answers in time")
    }

    /// Everything ln-hub sends until the response to `id`, and that response.
    fn until(&self, id: i64) -> (Vec<Value>, Value) {
        let mut before = vec![];
        loop {
            let message = self.next_message();
            if message.get("id") == Some(&json!(id)) && message.get("method").is_none() {
                return (before, message);
            }
            before.push(message);
        }
    }

    fn call(&mut self, method: &str, params: Value) -> (Vec<Value>, Value) {
        let id = self.send(method, params);
        self.until(id)
    }

    fn initialized(url: &str, token: &Path) -> Self {
        let mut editor = Editor::start(url, token);
        let (_, reply) = editor.call(
            "initialize",
            json!({"protocolVersion": 1, "clientCapabilities": {}}),
        );
        assert_eq!(reply["result"]["protocolVersion"], 1);
        editor
    }

    fn new_session(&mut self, cwd: &Path, mode: &str) -> String {
        let (_, reply) = self.call("session/new", json!({"cwd": cwd, "mcpServers": []}));
        let session = reply["result"]["sessionId"].as_str().unwrap().to_string();
        let commands = self.next_message();
        assert_eq!(
            commands["params"]["update"]["sessionUpdate"],
            "available_commands_update"
        );
        if mode != "status" {
            let (_, reply) = self.call(
                "session/set_mode",
                json!({"sessionId": session, "modeId": mode}),
            );
            assert_eq!(reply["result"], json!({}));
            self.next_message(); // the mode's commands
        }
        session
    }

    fn prompt(&mut self, session: &str, text: &str) -> (Vec<Value>, Value) {
        self.call(
            "session/prompt",
            json!({"sessionId": session, "prompt": [{"type": "text", "text": text}]}),
        )
    }
}

impl Drop for Editor {
    fn drop(&mut self) {
        let _ = self.child.kill();
        let _ = self.child.wait();
    }
}

fn updates<'a>(messages: &'a [Value], kind: &str) -> Vec<&'a Value> {
    messages
        .iter()
        .filter(|m| m["method"] == "session/update")
        .map(|m| &m["params"]["update"])
        .filter(|u| u["sessionUpdate"] == kind)
        .collect()
}

fn said(messages: &[Value]) -> String {
    updates(messages, "agent_message_chunk")
        .iter()
        .map(|u| u["content"]["text"].as_str().unwrap_or_default())
        .collect()
}

fn stop_reason(reply: &Value) -> &str {
    reply["result"]["stopReason"]
        .as_str()
        .unwrap_or_else(|| panic!("no stop reason: {reply}"))
}

fn replaying() -> Fake {
    Fake::start(
        |seen| match (seen.rpc(), seen.data().and_then(|d| d["skill"].as_str())) {
            ("SendStreamingMessage", _) => Reply::sse(fixture("start_run.sse")),
            ("SendMessage", Some("status")) => Reply::json(fixture("status.json")),
            ("SendMessage", Some("evidence")) => Reply::json(fixture("evidence.json")),
            ("SendMessage", Some("approval_document")) => {
                Reply::json(fixture("approval_document.json"))
            }
            ("ListTasks", _) => Reply::json(fixture("list_tasks.json")),
            ("CancelTask", _) => Reply::json(fixture("cancel.json")),
            _ => Reply::json(fixture("no_run.json")),
        },
    )
}

#[test]
fn initialize_advertises_only_what_it_implements() {
    let fake = replaying();
    let dir = scratch("acp-initialize");
    let mut editor = Editor::start(&fake.url, &token_file(&dir));
    let (_, reply) = editor.call(
        "initialize",
        json!({"protocolVersion": 1, "clientCapabilities": {}}),
    );
    let capabilities = &reply["result"]["agentCapabilities"];
    assert_eq!(capabilities["loadSession"], true);
    assert_eq!(capabilities["sessionCapabilities"]["list"], json!({}));
    assert_eq!(capabilities["promptCapabilities"]["image"], false);
    assert_eq!(reply["result"]["authMethods"], json!([]));
    assert_eq!(reply["result"]["agentInfo"]["name"], "ln-hub");
    let (_, reply) = editor.call("authenticate", json!({"methodId": "x"}));
    assert_eq!(reply["error"]["code"], -32601);
    assert!(fake.seen().is_empty());
}

#[test]
fn a_new_session_is_read_only_and_ignores_the_editors_mcp_servers() {
    let fake = replaying();
    let dir = scratch("acp-new");
    let mut editor = Editor::initialized(&fake.url, &token_file(&dir));
    let servers = json!([{"name": "fs", "command": "/bin/mcp-fs", "args": [], "env": []}]);
    let (_, reply) = editor.call("session/new", json!({"cwd": dir, "mcpServers": servers}));
    assert_eq!(reply["result"]["modes"]["currentModeId"], "status");
    let modes: Vec<&str> = reply["result"]["modes"]["availableModes"]
        .as_array()
        .unwrap()
        .iter()
        .map(|m| m["id"].as_str().unwrap())
        .collect();
    assert_eq!(modes, ["status", "design"]);
    let commands = editor.next_message();
    let names: Vec<&str> = commands["params"]["update"]["availableCommands"]
        .as_array()
        .unwrap()
        .iter()
        .map(|c| c["name"].as_str().unwrap())
        .collect();
    assert_eq!(names, ["runs", "status", "evidence", "help"]);
    assert!(fake.seen().is_empty(), "nothing reaches the orchestrator");
}

#[test]
fn start_streams_the_run_to_h1_as_a_plan_tool_calls_and_evidence() {
    let fake = replaying();
    let dir = scratch("acp-start");
    let spec = json!({"schema_version": "1.1", "task": {"objective": "x"}});
    std::fs::write(dir.join("spec.json"), spec.to_string()).unwrap();
    let mut editor = Editor::initialized(&fake.url, &token_file(&dir));
    let session = editor.new_session(&dir, "design");
    let (messages, reply) = editor.prompt(&session, "/start deploy-design spec.json");
    assert_eq!(stop_reason(&reply), "end_turn");

    let plans = updates(&messages, "plan");
    let last: Vec<(&str, &str)> = plans.last().unwrap()["entries"]
        .as_array()
        .unwrap()
        .iter()
        .map(|e| {
            (
                e["content"].as_str().unwrap(),
                e["status"].as_str().unwrap(),
            )
        })
        .collect();
    assert_eq!(last.first(), Some(&("G", "completed")));
    assert_eq!(last.last(), Some(&("H1", "in_progress")));
    let calls = updates(&messages, "tool_call");
    assert_eq!(calls.len(), 7, "one per recorded model call");
    let done = updates(&messages, "tool_call_update");
    assert!(done.iter().all(|u| u["status"] == "completed") && done.len() == 7);
    let text = said(&messages);
    assert!(
        text.contains("**Evidence**") && text.contains("survivors"),
        "{text}"
    );
    assert!(
        text.contains("needs a human") && text.contains("cannot approve"),
        "{text}"
    );

    let [sent] = fake.seen().try_into().unwrap();
    assert_eq!(sent.rpc(), "SendStreamingMessage");
    assert_eq!(
        sent.data().unwrap(),
        &json!({"skill": "start_run", "run_id": "deploy-design", "entry_spec": spec})
    );
    assert!(sent.body.pointer("/params/message/taskId").is_none());
}

#[test]
fn nothing_that_changes_a_run_reaches_the_orchestrator_from_status_mode() {
    let fake = replaying();
    let dir = scratch("acp-readonly");
    let mut editor = Editor::initialized(&fake.url, &token_file(&dir));
    let session = editor.new_session(&dir, "status");
    for text in [
        "/start r spec.json",
        "/advance r",
        "/approval-doc d ln-c because",
    ] {
        let (messages, reply) = editor.prompt(&session, text);
        assert_eq!(stop_reason(&reply), "end_turn");
        assert!(said(&messages).contains("read-only"), "{text}");
    }
    assert!(fake.seen().is_empty());
}

#[test]
fn approving_deciding_and_resuming_are_never_commands() {
    let fake = replaying();
    let dir = scratch("acp-human-only");
    let mut editor = Editor::initialized(&fake.url, &token_file(&dir));
    let session = editor.new_session(&dir, "design");
    for text in [
        "/approve",
        "/decide deploy-design",
        "/resume deploy-design",
        "/sign x",
    ] {
        let (messages, _) = editor.prompt(&session, text);
        let answer = said(&messages);
        assert!(
            answer.contains("ssh-keygen -Y sign") && answer.contains("run decide"),
            "{answer}"
        );
    }
    let (messages, _) = editor.prompt(&session, "please approve the design for me");
    assert!(said(&messages).contains("does not interpret free text"));
    assert!(fake.seen().is_empty());
}

#[test]
fn an_approval_document_is_written_only_after_the_editor_allows_it_once() {
    let fake = replaying();
    let dir = scratch("acp-approval-doc");
    let mut editor = Editor::initialized(&fake.url, &token_file(&dir));
    let session = editor.new_session(&dir, "design");
    let (messages, _) = editor.prompt(&session, "/status deploy-design");
    assert!(said(&messages).contains("**awaiting_human** at `H1`"));

    for (choice, written) in [("skip", false), ("write", true)] {
        let id = editor.send(
            "session/prompt",
            json!({"sessionId": session, "prompt": [{"type": "text",
                   "text": "/approval-doc approve_design_and_no_security_veto ln-c looks right"}]}),
        );
        let ask = loop {
            let message = editor.next_message();
            if message["method"] == "session/request_permission" {
                break message;
            }
        };
        let kinds: Vec<&str> = ask["params"]["options"]
            .as_array()
            .unwrap()
            .iter()
            .map(|o| o["kind"].as_str().unwrap())
            .collect();
        assert_eq!(
            kinds,
            ["allow_once", "reject_once"],
            "never an 'always' choice"
        );
        let title = ask["params"]["toolCall"]["title"].as_str().unwrap();
        assert!(
            title.contains("unsigned") && title.contains("approves nothing"),
            "{title}"
        );
        editor.write(json!({"jsonrpc": "2.0", "id": ask["id"],
                            "result": {"outcome": {"outcome": "selected", "optionId": choice}}}));
        let (messages, reply) = editor.until(id);
        assert_eq!(stop_reason(&reply), "end_turn");
        let text = said(&messages);
        assert_eq!(
            text.contains("**unsigned**") && text.contains("ssh-keygen -Y sign"),
            written,
            "{text}"
        );
    }
    let skills: Vec<String> = fake
        .seen()
        .iter()
        .filter_map(|s| s.data().map(|d| d["skill"].as_str().unwrap().to_string()))
        .collect();
    assert_eq!(skills, ["status", "approval_document"]);
    let last = fake.seen().pop().unwrap();
    assert!(last.body.pointer("/params/message/taskId").is_none());
    assert_eq!(last.data().unwrap()["run_id"], "deploy-design");
}

#[test]
fn cancel_ends_the_turn_at_once_and_cancels_the_task() {
    // The stream sends the task and one update, then stays open as a long model call would.
    let head: String = fixture("start_run.sse")
        .split_inclusive("\r\n\r\n")
        .take(2)
        .collect();
    let fake = Fake::start(move |seen| match seen.rpc() {
        "SendStreamingMessage" => Reply::stalled_sse(head.clone(), Duration::from_secs(30)),
        "CancelTask" => Reply::json(fixture("cancel.json")),
        _ => Reply::json(fixture("no_run.json")),
    });
    let dir = scratch("acp-cancel");
    let mut editor = Editor::initialized(&fake.url, &token_file(&dir));
    let session = editor.new_session(&dir, "design");
    let id = editor.send(
        "session/prompt",
        json!({"sessionId": session, "prompt": [{"type": "text", "text": "/advance deploy-design"}]}),
    );
    loop {
        let message = editor.next_message();
        if said(&[message]).contains("advancing") {
            break;
        }
    }
    let asked = Instant::now();
    editor.write(
        json!({"jsonrpc": "2.0", "method": "session/cancel", "params": {"sessionId": session}}),
    );
    let (_, reply) = editor.until(id);
    assert_eq!(stop_reason(&reply), "cancelled");
    assert!(
        asked.elapsed() < Duration::from_secs(5),
        "answered while the stream was still open"
    );
    let deadline = Instant::now() + WAIT;
    while !fake.methods().contains(&"CancelTask".to_string()) {
        assert!(Instant::now() < deadline, "CancelTask was never sent");
        thread::sleep(Duration::from_millis(20));
    }
    let cancel = fake
        .seen()
        .into_iter()
        .find(|s| s.rpc() == "CancelTask")
        .unwrap();
    assert_eq!(
        cancel.body["params"]["id"],
        "07a36a8a-495a-418f-aee9-8807260a1beb"
    );
    // The session takes the next prompt.
    let (messages, reply) = editor.prompt(&session, "/help");
    assert_eq!(stop_reason(&reply), "end_turn");
    assert!(said(&messages).contains("/advance"));
}

#[test]
fn a_cancel_before_the_task_exists_still_cancels_it() {
    // The server answers only after a while (as while it creates the run), then streams.
    let head: String = fixture("start_run.sse")
        .split_inclusive("\r\n\r\n")
        .take(2)
        .collect();
    let fake = Fake::start(move |seen| match seen.rpc() {
        "SendStreamingMessage" => Reply {
            delay: Some(Duration::from_secs(2)),
            ..Reply::stalled_sse(head.clone(), Duration::from_secs(30))
        },
        "CancelTask" => Reply::json(fixture("cancel.json")),
        _ => Reply::json(fixture("no_run.json")),
    });
    let dir = scratch("acp-early-cancel");
    let mut editor = Editor::initialized(&fake.url, &token_file(&dir));
    let session = editor.new_session(&dir, "design");
    let id = editor.send(
        "session/prompt",
        json!({"sessionId": session, "prompt": [{"type": "text", "text": "/advance deploy-design"}]}),
    );
    while !fake.methods().contains(&"SendStreamingMessage".to_string()) {
        thread::sleep(Duration::from_millis(10));
    }
    let asked = Instant::now();
    editor.write(
        json!({"jsonrpc": "2.0", "method": "session/cancel", "params": {"sessionId": session}}),
    );
    let (messages, reply) = editor.until(id);
    assert_eq!(stop_reason(&reply), "cancelled");
    assert!(
        asked.elapsed() < Duration::from_secs(1),
        "answered before the server replied"
    );
    assert!(
        said(&messages).is_empty(),
        "nothing shown after the cancel: {messages:?}"
    );
    let deadline = Instant::now() + WAIT;
    while !fake.methods().contains(&"CancelTask".to_string()) {
        assert!(Instant::now() < deadline, "CancelTask was never sent");
        thread::sleep(Duration::from_millis(20));
    }
    let cancel = fake
        .seen()
        .into_iter()
        .find(|s| s.rpc() == "CancelTask")
        .unwrap();
    assert_eq!(
        cancel.body["params"]["id"],
        "07a36a8a-495a-418f-aee9-8807260a1beb"
    );
    assert_eq!(
        fake.methods().iter().filter(|m| *m == "CancelTask").count(),
        1
    );
}

#[test]
fn the_session_list_is_the_run_list_and_a_run_loads_as_its_status() {
    let fake = replaying();
    let dir = scratch("acp-list");
    let mut editor = Editor::initialized(&fake.url, &token_file(&dir));
    let (_, reply) = editor.call("session/list", json!({"cwd": dir}));
    let sessions = reply["result"]["sessions"].as_array().unwrap();
    assert_eq!(sessions.len(), 1);
    assert_eq!(sessions[0]["sessionId"], "run/deploy-design");
    assert!(
        sessions[0]["title"]
            .as_str()
            .unwrap()
            .starts_with("deploy-design")
    );

    let (messages, reply) = editor.call(
        "session/load",
        json!({"sessionId": "run/deploy-design", "cwd": dir, "mcpServers": []}),
    );
    assert_eq!(reply["result"]["modes"]["currentModeId"], "status");
    assert!(said(&messages).contains("Run `deploy-design`: **awaiting_human**"));
    let (_, reply) = editor.call(
        "session/load",
        json!({"sessionId": "ln-1-1", "cwd": dir, "mcpServers": []}),
    );
    assert!(
        reply["error"]["message"]
            .as_str()
            .unwrap()
            .contains("not a run session")
    );
}

#[test]
fn a_missing_token_is_explained_and_a_remote_endpoint_is_refused() {
    let fake = replaying();
    let dir = scratch("acp-no-token");
    let mut editor = Editor::initialized(&fake.url, &dir.join("absent-token"));
    let session = editor.new_session(&dir, "status");
    let (messages, reply) = editor.prompt(&session, "/runs");
    assert_eq!(stop_reason(&reply), "end_turn");
    assert!(said(&messages).contains("serve a2a"), "{}", said(&messages));
    assert!(fake.seen().is_empty());

    let out = Command::new(env!("CARGO_BIN_EXE_ln-hub"))
        .args(["acp", "--a2a-url", "http://10.0.0.1:8420/a2a/jsonrpc"])
        .stdin(Stdio::null())
        .output()
        .unwrap();
    assert!(!out.status.success());
    assert!(String::from_utf8_lossy(&out.stderr).contains("not a loopback address"));
    assert!(out.stdout.is_empty());
}

#[test]
fn the_token_never_goes_through_a_proxy_from_the_environment() {
    let fake = replaying();
    let proxy = Fake::start(|_| Reply::json("{}"));
    let via = proxy.url.trim_end_matches("/a2a/jsonrpc").to_string();
    let env: Vec<(&str, &str)> = ["HTTP_PROXY", "http_proxy", "ALL_PROXY", "all_proxy"]
        .into_iter()
        .map(|k| (k, via.as_str()))
        .chain([("NO_PROXY", ""), ("no_proxy", "")])
        .collect();
    let dir = scratch("acp-proxy");
    let mut editor = Editor::start_with(&fake.url, &token_file(&dir), &env);
    editor.call(
        "initialize",
        json!({"protocolVersion": 1, "clientCapabilities": {}}),
    );
    let session = editor.new_session(&dir, "status");
    let (messages, _) = editor.prompt(&session, "/runs");
    assert!(
        said(&messages).contains("deploy-design"),
        "{}",
        said(&messages)
    );
    assert_eq!(fake.methods(), ["ListTasks"]);
    assert!(proxy.seen().is_empty(), "the proxy saw {:?}", proxy.seen());
}

#[test]
fn the_acp_and_a2a_crates_never_start_a_process() {
    // `Command` as a whole word: `AvailableCommand::new` is an ACP type, not a process.
    let spawns = |source: &str| {
        source.contains("process::Command")
            || source
                .match_indices("Command::new")
                .any(|(at, _)| !source[..at].ends_with(|c: char| c.is_alphanumeric() || c == '_'))
    };
    assert!(spawns("let c = Command::new(\"sh\");") && !spawns("AvailableCommand::new(n, d)"));
    let crates = Path::new(env!("CARGO_MANIFEST_DIR")).join("..");
    for name in ["ln-acp", "ln-a2a"] {
        for entry in std::fs::read_dir(crates.join(name).join("src")).unwrap() {
            let path = entry.unwrap().path();
            let source = std::fs::read_to_string(&path).unwrap();
            assert!(!spawns(&source), "{} starts a process", path.display());
        }
    }
}
