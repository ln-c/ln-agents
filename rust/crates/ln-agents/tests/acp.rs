//! `ln-agents acp` as an editor sees it: the binary on stdio, against a fake `ln-pipeline` that
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
        let mut child = Command::new(env!("CARGO_BIN_EXE_ln-agents"))
            .args(["acp", "--a2a-url", url, "--token-file"])
            .arg(token)
            .env_remove("LN_AGENTS_A2A_URL")
            .env_remove("LN_AGENTS_TOKEN_FILE")
            .envs(env.iter().copied())
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .spawn()
            .expect("ln-agents starts");
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
            .expect("ln-agents answers in time")
    }

    /// Everything ln-agents sends until the response to `id`, and that response.
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
            ("SendMessage", Some("ask")) => Reply::json(fixture("ask.json")),
            ("SendMessage", Some("models")) => Reply::json(fixture("models.json")),
            ("SendMessage", Some("guideline")) => Reply::json(fixture("guideline.json")),
            ("SendMessage", Some("complete_mapping")) => {
                Reply::json(fixture("complete_mapping_none.json"))
            }
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
    assert_eq!(reply["result"]["agentInfo"]["name"], "ln-agents");
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
    assert_eq!(
        names,
        [
            "runs",
            "status",
            "evidence",
            "models",
            "agents",
            "guideline",
            "help"
        ]
    );
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
    // The coordinator's summary of the stop: shown as the message, its file one click away, and
    // the server's own progress line before it.
    assert!(text.contains("writing the summary of this stop"), "{text}");
    assert!(
        text.contains("H1: waiting for a signed human decision")
            && text.contains("## What to do next"),
        "{text}"
    );
    assert!(
        !text.contains("<!-- ln-agents"),
        "the file's comment is not for the chat"
    );
    let links: Vec<&str> = updates(&messages, "agent_message_chunk")
        .iter()
        .filter(|u| u["content"]["type"] == "resource_link")
        .filter_map(|u| u["content"]["uri"].as_str())
        .collect();
    assert_eq!(
        links,
        ["file:///work/.runs/deploy-design/views/STOP-001-H1.md"]
    );
    assert!(
        text.contains("needs a human") && text.contains("cannot approve"),
        "{text}"
    );

    // the server is asked about the mapping first; every agent has a model, so the run starts
    let [gate, sent] = fake.seen().try_into().unwrap();
    assert_eq!(gate.rpc(), "SendMessage");
    assert_eq!(gate.data().unwrap(), &json!({"skill": "complete_mapping"}));
    assert_eq!(sent.rpc(), "SendStreamingMessage");
    assert_eq!(
        sent.data().unwrap(),
        &json!({"skill": "start_run", "run_id": "deploy-design", "entry_spec": spec})
    );
    assert!(sent.body.pointer("/params/message/taskId").is_none());
}

/// A fake orchestrator that says an agent has no model, with the reply the real server gave.
fn gap_fake(fixture_name: &'static str) -> Fake {
    Fake::start(
        move |seen| match seen.data().and_then(|d| d["skill"].as_str()) {
            Some("complete_mapping") => Reply::json(fixture(fixture_name)),
            _ => Reply::json(fixture("no_run.json")),
        },
    )
}

#[test]
fn start_proposes_a_mapping_instead_of_a_run_when_an_agent_has_no_model() {
    let fake = gap_fake("complete_mapping.json");
    let dir = scratch("acp-gap");
    let mut editor = Editor::initialized(&fake.url, &token_file(&dir));
    let session = editor.new_session(&dir, "design");
    // the entry spec does not exist: the gate comes before it is read
    let (messages, reply) = editor.prompt(&session, "/start r missing.json");
    assert_eq!(stop_reason(&reply), "end_turn");
    let text = said(&messages);
    assert!(
        text.contains("**1 agent has no model**, so no run was started"),
        "{text}"
    );
    assert!(
        text.contains(
            "`agents/pipeline-extra-helper-agent.md`: no entry under `agents` owns this file"
        ),
        "{text}"
    );
    assert!(text.contains("no model chose this"), "{text}");
    assert!(
        text.contains("primary: gemini_3_8_flash_high"),
        "the diff is shown: {text}"
    );
    assert!(text.contains("`/confirm d10d897d`"), "{text}");
    assert!(text.contains("run `/start` again"), "{text}");
    assert!(
        !text.contains("acting as the authority"),
        "no model proposed it: {text}"
    );
    let seen = fake.seen();
    assert!(
        seen.iter().all(|s| s.rpc() == "SendMessage"),
        "no stream was opened"
    );
    assert_eq!(skills_sent(&fake), [json!({"skill": "complete_mapping"})]);
}

#[test]
fn start_says_why_there_is_no_proposal_and_still_starts_nothing() {
    let fake = gap_fake("complete_mapping_refused.json");
    let dir = scratch("acp-gap-refused");
    let mut editor = Editor::initialized(&fake.url, &token_file(&dir));
    let session = editor.new_session(&dir, "design");
    let (messages, reply) = editor.prompt(&session, "/start r spec.json");
    assert_eq!(stop_reason(&reply), "end_turn");
    let text = said(&messages);
    assert!(text.contains("**1 agent has no model**"), "{text}");
    assert!(text.contains("Not drafted, so nothing changed"), "{text}");
    assert!(
        text.contains("names no coordinator") && text.contains("`coordinator:`"),
        "{text}"
    );
    assert!(!text.contains("/confirm"), "{text}");
    assert_eq!(skills_sent(&fake), [json!({"skill": "complete_mapping"})]);
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
        "/decide",
        "/resume ln-c because",
        "/add_model google_ai_pro gemini-9-new",
        "/remove_model gemini_9_new",
        "/agents design.proposal_b.primary gemini_4_argon_high",
        "/agents auto use only local models",
        "/confirm 1eb25dbc",
        "/discard 1eb25dbc",
    ] {
        let (messages, reply) = editor.prompt(&session, text);
        assert_eq!(stop_reason(&reply), "end_turn");
        assert!(said(&messages).contains("read-only"), "{text}");
    }
    assert!(fake.seen().is_empty());
}

#[test]
fn models_shows_the_catalog_in_status_mode_for_no_run() {
    let fake = replaying();
    let dir = scratch("acp-models");
    let mut editor = Editor::initialized(&fake.url, &token_file(&dir));
    let session = editor.new_session(&dir, "status");
    let (messages, reply) = editor.prompt(&session, "/models");
    assert_eq!(stop_reason(&reply), "end_turn");
    let text = said(&messages);
    // a model in use: where it runs, what it is bound to and what is not yet verified
    assert!(text.contains("| `qwen3_5_9b` | llamacpp_local:qwen3.5-9b | qwen3_5 | local |"));
    assert!(text.contains("C1.module_decomposition, D1.test_generation, E1.implementation_patch"));
    assert!(text.contains("not ready: qwen3_5_9b.daemon UNVERIFIED"));
    assert!(text.contains("| missing: no record |"), "{text}");
    // the rest, by name: bindable ones show what they declare, unclaimed ones say they cannot bind
    assert!(text.contains("`clef_flash` (classification)"));
    assert!(text.contains("Known, unclaimed"));
    assert!(text.contains("`gemini_4_argon_high`"));
    let sent: Vec<Value> = fake
        .seen()
        .iter()
        .filter_map(|s| s.data().cloned())
        .collect();
    assert_eq!(
        sent,
        [json!({"skill": "models"})],
        "no run id, nothing else"
    );
}

#[test]
fn guideline_walks_a_new_user_to_a_start_in_status_mode_for_no_run() {
    let fake = replaying();
    let dir = scratch("acp-guideline");
    let mut editor = Editor::initialized(&fake.url, &token_file(&dir));
    let session = editor.new_session(&dir, "status");
    let (messages, reply) = editor.prompt(&session, "/guideline");
    assert_eq!(stop_reason(&reply), "end_turn");
    let text = said(&messages);
    assert!(text.contains("**Not ready.**"), "{text}");
    // the user names the coordinator; the rest is the pipeline's, or says what is in the way
    assert!(
        text.contains("1. **Name a coordinator in LN-AGENTS.md**: to do"),
        "{text}"
    );
    assert!(
        text.contains("   ---\n   coordinator: <model>\n   ---\n"),
        "{text}"
    );
    assert!(
        text.contains("**Every agent has a model**: to do"),
        "{text}"
    );
    assert!(
        text.contains("Name a coordinator first (LN-AGENTS.md)"),
        "{text}"
    );
    assert!(
        text.contains("**Local model servers can be started**: automatic"),
        "{text}"
    );
    assert!(text.contains("Not checked here:"), "{text}");
    let sent: Vec<Value> = fake
        .seen()
        .iter()
        .filter_map(|s| s.data().cloned())
        .collect();
    assert_eq!(
        sent,
        [json!({"skill": "guideline"})],
        "no run id, nothing else"
    );
}

/// A fake orchestrator that answers each registry skill with what the real server sent.
fn registry_fake() -> Fake {
    Fake::start(|seen| {
        let data = seen.data();
        match data.and_then(|d| d["skill"].as_str()) {
            Some("agents") => Reply::json(fixture("agents.json")),
            Some("status") => Reply::json(fixture("status.json")),
            Some("ask") => Reply::json(fixture("ask.json")),
            Some("apply_change") => Reply::json(fixture("applied.json")),
            Some("discard_change") => Reply::json(fixture("discarded.json")),
            Some("propose_mapping") => {
                if data.unwrap().get("intent").is_some() {
                    Reply::json(fixture("proposal.json"))
                } else {
                    Reply::json(fixture("proposal_refused.json"))
                }
            }
            Some("registry_change") => match data.unwrap()["kind"].as_str() {
                Some("add_model") => Reply::json(fixture("change.json")),
                Some("remove_model") => Reply::json(fixture("change_remove.json")),
                _ => Reply::json(fixture("change_refused.json")),
            },
            _ => Reply::json(fixture("no_run.json")),
        }
    })
}

fn skills_sent(fake: &Fake) -> Vec<Value> {
    fake.seen()
        .iter()
        .filter_map(|s| s.data().cloned())
        .collect()
}

#[test]
fn agents_shows_each_slot_and_the_drafts_waiting_in_status_mode() {
    let fake = registry_fake();
    let dir = scratch("acp-agents");
    let mut editor = Editor::initialized(&fake.url, &token_file(&dir));
    let session = editor.new_session(&dir, "status");
    let (messages, reply) = editor.prompt(&session, "/agents");
    assert_eq!(stop_reason(&reply), "end_turn");
    let text = said(&messages);
    assert!(text.contains(
        "| `design.proposal_b.primary` | `gemini_4_argon_medium` | gemini | cloud | A1.proposal_b, A2.critique_a_by_b |"
    ), "{text}");
    assert!(
        text.contains("| `design.synthesis.primary` | deterministic_orchestrator (not a model) |")
    );
    assert!(text.contains("`human_authority.model_assistance` | `claude_cli_default`, `gemini_3_8_flash_high`, `qwen3_5_9b`"));
    assert!(
        text.contains("#### Waiting for you") && text.contains("`/confirm 1eb25dbc`"),
        "{text}"
    );
    assert_eq!(skills_sent(&fake), [json!({"skill": "agents"})]);
}

#[test]
fn a_draft_shows_its_diff_and_nothing_is_written_until_the_human_types_confirm() {
    let fake = registry_fake();
    let dir = scratch("acp-draft");
    let mut editor = Editor::initialized(&fake.url, &token_file(&dir));
    let session = editor.new_session(&dir, "design");
    let (messages, reply) = editor.prompt(
        &session,
        "/add_model google_ai_pro gemini-9-new design_reasoning",
    );
    assert_eq!(stop_reason(&reply), "end_turn");
    let text = said(&messages);
    assert!(
        text.contains("**Add model gemini_9_new (google_ai_pro:gemini-9-new)**"),
        "{text}"
    );
    assert!(text.contains("```diff") && text.contains("+  gemini_9_new: {provider: google_ai_pro"));
    assert!(text.contains("You claim these capabilities for it: design_reasoning"));
    assert!(text.contains("type `/confirm 1eb25dbc`") && text.contains("`/discard 1eb25dbc`"));
    assert!(text.contains("you commit and push it signed"));
    assert_eq!(
        skills_sent(&fake),
        [
            json!({"skill": "registry_change", "kind": "add_model", "provider": "google_ai_pro",
                "model_id": "gemini-9-new", "capabilities": ["design_reasoning"]})
        ],
        "drafting sends the fields and nothing else; no apply"
    );

    let (messages, _) = editor.prompt(&session, "/confirm 1eb25dbc");
    let text = said(&messages);
    assert!(text.contains("Written: Add model gemini_9_new"), "{text}");
    assert!(text.contains("not committed") && text.contains("a run in progress keeps the policy"));
    assert_eq!(
        skills_sent(&fake).last().unwrap(),
        &json!({"skill": "apply_change", "change_id": "1eb25dbc"}),
        "a confirmation names a draft and carries no text"
    );
}

#[test]
fn a_refused_draft_lists_the_rules_and_offers_no_confirm() {
    let fake = registry_fake();
    let dir = scratch("acp-refused");
    let mut editor = Editor::initialized(&fake.url, &token_file(&dir));
    let session = editor.new_session(&dir, "design");
    let (messages, _) = editor.prompt(
        &session,
        "/agents design.proposal_b.primary claude_cli_default",
    );
    let text = said(&messages);
    assert!(text.contains("Not drafted, so nothing changed"), "{text}");
    assert!(text.contains("registry R14 A1.proposal_b"));
    assert!(
        !text.contains("/confirm"),
        "a refused change has nothing to confirm"
    );
    assert_eq!(
        skills_sent(&fake),
        [
            json!({"skill": "registry_change", "kind": "bind", "slot": "design.proposal_b.primary",
                "models": ["claude_cli_default"]})
        ]
    );
}

#[test]
fn the_authoritys_proposal_is_shown_as_its_words_beside_a_diff_you_confirm() {
    let fake = registry_fake();
    let dir = scratch("acp-propose");
    let mut editor = Editor::initialized(&fake.url, &token_file(&dir));
    let session = editor.new_session(&dir, "design");
    let (messages, reply) = editor.prompt(
        &session,
        "/agents auto use a third model family for design B",
    );
    assert_eq!(stop_reason(&reply), "end_turn");
    let text = said(&messages);
    assert!(
        text.contains("Proposed by `gemini_3_8_flash_high`"),
        "{text}"
    );
    assert!(
        text.contains("the authority says: Design B moves to GPT-OSS."),
        "{text}"
    );
    assert!(text.contains("Bind 1 slot (proposed by gemini_3_8_flash_high)"));
    assert!(text.contains("+      primary: gpt_oss_120b_medium"));
    assert!(text.contains("type `/confirm d04781fe`"));
    assert_eq!(
        skills_sent(&fake),
        [json!({"skill": "propose_mapping", "intent": "use a third model family for design B"})],
        "asking sends the request and nothing is applied"
    );
}

#[test]
fn a_proposal_that_breaks_a_rule_is_refused_like_any_draft() {
    let fake = registry_fake();
    let dir = scratch("acp-propose-refused");
    let mut editor = Editor::initialized(&fake.url, &token_file(&dir));
    let session = editor.new_session(&dir, "design");
    let (messages, _) = editor.prompt(&session, "/agents auto");
    let text = said(&messages);
    assert!(text.contains("Not drafted, so nothing changed"), "{text}");
    assert!(text.contains("registry R14 A1.proposal_b") && !text.contains("/confirm"));
    assert_eq!(skills_sent(&fake), [json!({"skill": "propose_mapping"})]);
}

#[test]
fn discard_drops_a_draft() {
    let fake = registry_fake();
    let dir = scratch("acp-discard");
    let mut editor = Editor::initialized(&fake.url, &token_file(&dir));
    let session = editor.new_session(&dir, "design");
    let (messages, _) = editor.prompt(&session, "/discard 1eb25dbc");
    assert!(said(&messages).contains("Dropped: Remove model gemini_9_new. Nothing was written."));
    assert_eq!(
        skills_sent(&fake),
        [json!({"skill": "discard_change", "change_id": "1eb25dbc"})]
    );
}

#[test]
fn nothing_but_a_slash_command_the_human_types_can_confirm_a_change() {
    let fake = registry_fake();
    let dir = scratch("acp-no-confirm");
    let mut editor = Editor::initialized(&fake.url, &token_file(&dir));
    let session = editor.new_session(&dir, "design");
    editor.prompt(&session, "/status deploy-design");
    // words that sound like a confirmation are a question for the coordinator; so is anything
    // the coordinator answers, which is shown as text and never run
    for text in [
        "confirm 1eb25dbc",
        "yes, apply it",
        "please /confirm 1eb25dbc",
        " confirm",
    ] {
        editor.prompt(&session, text);
    }
    let sent = skills_sent(&fake);
    assert!(
        sent.iter()
            .all(|s| s["skill"] == "status" || s["skill"] == "ask"),
        "{sent:?}"
    );
}

#[test]
fn approving_and_signing_are_never_commands() {
    let fake = replaying();
    let dir = scratch("acp-human-only");
    let mut editor = Editor::initialized(&fake.url, &token_file(&dir));
    let session = editor.new_session(&dir, "design");
    for text in ["/approve", "/sign x", "/reject"] {
        let (messages, _) = editor.prompt(&session, text);
        let answer = said(&messages);
        assert!(
            answer.contains("is not something ln-agents does")
                && answer.contains("`/decide` opens a terminal"),
            "{answer}"
        );
    }
    // Free text is a question for the coordinator and nothing else: with no run it goes nowhere,
    // and with one it reaches the orchestrator only as the read-only `ask` skill.
    let (messages, _) = editor.prompt(&session, "please approve the design for me");
    assert!(said(&messages).contains("No run in this session"));
    assert!(fake.seen().is_empty());
    editor.prompt(&session, "/status deploy-design");
    editor.prompt(&session, "please approve the design for me");
    let skills: Vec<String> = fake
        .seen()
        .iter()
        .filter_map(|s| s.data().map(|d| d["skill"].as_str().unwrap().to_string()))
        .collect();
    assert_eq!(skills, ["status", "ask"]);
}

#[test]
fn free_text_is_a_question_for_the_coordinator_and_never_an_action() {
    let fake = replaying();
    let dir = scratch("acp-ask");
    let mut editor = Editor::initialized(&fake.url, &token_file(&dir));
    let session = editor.new_session(&dir, "status"); // read-only is enough to ask
    let (messages, reply) = editor.prompt(&session, "why is the synthesis first?");
    assert_eq!(stop_reason(&reply), "end_turn");
    assert!(said(&messages).contains("No run in this session"));
    assert!(
        fake.seen().is_empty(),
        "no run to ask about, so nothing is sent"
    );

    editor.prompt(&session, "/status deploy-design");
    let (messages, reply) = editor.prompt(&session, "why is the synthesis first?");
    assert_eq!(stop_reason(&reply), "end_turn");
    let text = said(&messages);
    assert!(text.contains("The synthesis ranks first"), "{text}");
    assert!(
        text.contains("_From: STOP-001-H1.md, H1-evidence.md, A3-synthesis.md_"),
        "{text}"
    );
    assert!(
        updates(&messages, "tool_call").is_empty()
            && !messages
                .iter()
                .any(|m| m["method"] == "session/request_permission"),
        "an answer is text: no tool call, no permission to give"
    );
    let asked = fake.seen().pop().unwrap();
    assert_eq!(
        asked.data().unwrap(),
        &json!({"skill": "ask", "run_id": "deploy-design", "question": "why is the synthesis first?"})
    );
    let (messages, _) = editor.prompt(&session, "help");
    assert!(said(&messages).contains("Anything else you type is a question"));
    assert_eq!(fake.seen().len(), 2, "help is local");
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

    let out = Command::new(env!("CARGO_BIN_EXE_ln-agents"))
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

/// Answer the permission request of `text` with `choice`; the turn's messages and its reply.
fn prompt_and_answer(editor: &mut Editor, session: &str, text: &str, choice: &str) -> Vec<Value> {
    let id = editor.send(
        "session/prompt",
        json!({"sessionId": session, "prompt": [{"type": "text", "text": text}]}),
    );
    let ask = loop {
        let message = editor.next_message();
        if message["method"] == "session/request_permission" {
            break message;
        }
    };
    editor.write(json!({"jsonrpc": "2.0", "id": ask["id"],
                        "result": {"outcome": {"outcome": "selected", "optionId": choice}}}));
    editor.until(id).0
}

/// A session over a fake orchestrator whose `approval_document` reply names a document under
/// `dir`, with `LN_AGENTS_TERMINAL` and `LN_AGENTS_PIPELINE` set as given.
fn human_session(dir: &Path, terminal: &str) -> (Fake, Editor, String, std::path::PathBuf) {
    let runs = dir.join("runs");
    let document = runs.join("deploy-design/approvals/H1-approve_design_and_no_security_veto.json");
    std::fs::create_dir_all(document.parent().unwrap()).unwrap();
    std::fs::write(&document, "{}").unwrap();
    // As JSON text: a Windows path has backslashes.
    let runs_json = serde_json::to_string(runs.to_str().unwrap()).unwrap();
    let reply =
        fixture("approval_document.json").replace("/work/runs", runs_json.trim_matches('"'));
    let fake = Fake::start(move |seen| {
        match (seen.rpc(), seen.data().and_then(|d| d["skill"].as_str())) {
            ("SendMessage", Some("status")) => Reply::json(fixture("status.json")),
            ("SendMessage", Some("ask")) => Reply::json(fixture("ask.json")),
            ("SendMessage", Some("approval_document")) => Reply::json(reply.clone()),
            _ => Reply::json(fixture("no_run.json")),
        }
    });
    let env = [
        ("LN_AGENTS_TERMINAL", terminal),
        ("LN_AGENTS_PIPELINE", "pipe cmd"),
    ];
    let mut editor = Editor::start_with(&fake.url, &token_file(dir), &env);
    editor.call(
        "initialize",
        json!({"protocolVersion": 1, "clientCapabilities": {}}),
    );
    let session = editor.new_session(dir, "design");
    editor.prompt(&session, "/status deploy-design");
    (fake, editor, session, document)
}

#[cfg(unix)]
fn fake_terminal(dir: &Path) -> (std::path::PathBuf, std::path::PathBuf) {
    use std::os::unix::fs::PermissionsExt;
    let record = dir.join("terminal-arguments.txt");
    let script = dir.join("fake-terminal");
    std::fs::write(
        &script,
        format!(
            "#!/bin/sh\nprintf '%s\\n' \"$@\" > '{}'\n",
            record.display()
        ),
    )
    .unwrap();
    std::fs::set_permissions(&script, std::fs::Permissions::from_mode(0o755)).unwrap();
    (script, record)
}

#[cfg(unix)]
fn opened_with(record: &Path) -> Vec<String> {
    let started = Instant::now();
    while !record.exists() && started.elapsed() < WAIT {
        thread::sleep(Duration::from_millis(20));
    }
    std::fs::read_to_string(record)
        .expect("the terminal was opened")
        .lines()
        .map(String::from)
        .collect()
}

#[cfg(unix)]
#[test]
fn decide_opens_a_terminal_for_the_document_that_was_written_and_for_nothing_else() {
    let dir = scratch("acp-decide");
    let (terminal, record) = fake_terminal(&dir);
    let (_fake, mut editor, session, document) = human_session(&dir, terminal.to_str().unwrap());

    // No document was written in this session: nothing to sign, nothing opened.
    let (messages, _) = editor.prompt(&session, "/decide");
    assert!(
        said(&messages).contains("No unsigned document"),
        "{messages:?}"
    );
    // Words are not commands, whatever they ask for.
    for text in [
        "please sign it for me",
        "decide the design and resume the run",
    ] {
        editor.prompt(&session, text);
    }
    assert!(!record.exists());

    prompt_and_answer(
        &mut editor,
        &session,
        "/approval-doc approve_design_and_no_security_veto ln-c looks right",
        "write",
    );
    assert!(!record.exists(), "writing the document opens nothing");
    let (messages, reply) = editor.prompt(&session, "/decide");
    assert_eq!(stop_reason(&reply), "end_turn");
    let text = said(&messages);
    assert!(text.contains("A terminal window is open"), "{text}");
    assert!(text.contains("never sees"), "{text}");

    let words = opened_with(&record);
    let exe = std::path::PathBuf::from(&words[0]);
    assert_eq!(
        exe.canonicalize().unwrap(),
        Path::new(env!("CARGO_BIN_EXE_ln-agents"))
            .canonicalize()
            .unwrap()
    );
    assert_eq!(
        words[1..],
        [
            "decide",
            "deploy-design",
            "--approval",
            document.to_str().unwrap(),
            "--namespace",
            "agentic-pipeline-approval",
            "--runs-dir",
            dir.join("runs").to_str().unwrap(),
            "--",
            "pipe",
            "cmd",
        ]
    );
}

#[cfg(unix)]
#[test]
fn resume_opens_a_terminal_that_runs_the_orchestrators_own_resume() {
    let dir = scratch("acp-resume");
    let (terminal, record) = fake_terminal(&dir);
    let (_fake, mut editor, session, _) = human_session(&dir, terminal.to_str().unwrap());
    let (messages, _) = editor.prompt(&session, "/resume ln-c quota is back");
    assert!(said(&messages).contains("A terminal window is open"));
    let words = opened_with(&record);
    assert_eq!(
        words[1..],
        [
            "resume",
            "deploy-design",
            "--by",
            "ln-c",
            "--reason",
            "quota is back",
            "--runs-dir",
            dir.join(".runs").to_str().unwrap(),
            "--",
            "pipe",
            "cmd",
        ]
    );
}

#[test]
fn without_a_terminal_the_human_is_told_the_command_to_run_by_hand() {
    let dir = scratch("acp-no-terminal");
    let missing = dir.join("no-such-terminal");
    let (_fake, mut editor, session, _) = human_session(&dir, missing.to_str().unwrap());
    let (messages, _) = editor.prompt(&session, "/resume ln-c quota is back");
    let text = said(&messages);
    assert!(text.contains("No terminal window was opened"), "{text}");
    assert!(
        text.contains("pipe cmd run resume deploy-design --by ln-c --reason 'quota is back'"),
        "{text}"
    );

    prompt_and_answer(
        &mut editor,
        &session,
        "/approval-doc approve_design_and_no_security_veto ln-c looks right",
        "write",
    );
    let (messages, _) = editor.prompt(&session, "/decide");
    let text = said(&messages);
    assert!(
        text.contains("ssh-keygen -Y sign") && text.contains("run decide deploy-design"),
        "{text}"
    );
}

#[test]
fn an_orchestrator_that_does_not_say_the_namespace_is_reported_at_once_not_at_decide() {
    let dir = scratch("acp-old-server");
    let mut reply: Value = serde_json::from_str(&fixture("approval_document.json")).unwrap();
    let data = reply.pointer_mut("/result/message/parts/0/data").unwrap();
    data.as_object_mut().unwrap().remove("namespace");
    let reply = reply.to_string();
    let fake = Fake::start(move |seen| {
        match (seen.rpc(), seen.data().and_then(|d| d["skill"].as_str())) {
            ("SendMessage", Some("status")) => Reply::json(fixture("status.json")),
            ("SendMessage", Some("approval_document")) => Reply::json(reply.clone()),
            _ => Reply::json(fixture("no_run.json")),
        }
    });
    let mut editor = Editor::initialized(&fake.url, &token_file(&dir));
    let session = editor.new_session(&dir, "design");
    editor.prompt(&session, "/status deploy-design");
    let messages = prompt_and_answer(
        &mut editor,
        &session,
        "/approval-doc approve_design_and_no_security_veto ln-c looks right",
        "write",
    );
    let text = said(&messages);
    assert!(
        text.contains("**unsigned**"),
        "the document is still shown: {text}"
    );
    assert!(
        text.contains("`/decide` cannot use this document") && text.contains("restart `serve a2a`"),
        "{text}"
    );
}
