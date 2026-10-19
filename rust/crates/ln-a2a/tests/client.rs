//! The A2A client against the wire format the real server produced (fixtures captured from
//! `a2a_server.py` over the playback recording) and against a fake server.

mod fake;

use a2a::{AgentCard, PartContent, SendMessageResponse, StreamResponse, TaskState};
use fake::{Fake, Reply, TOKEN, fixture, scratch, token_file};
use ln_a2a::{Client, Endpoint, Error, Events, Request, data, loopback_origin, run_id, text};
use serde_json::{Value, json};

fn result(name: &str) -> Value {
    let reply: Value = serde_json::from_str(&fixture(name)).unwrap();
    reply["result"].clone()
}

#[test]
fn every_captured_reply_deserializes() {
    let card: AgentCard = serde_json::from_str(&fixture("card.json")).unwrap();
    assert_eq!(card.name, "ln-pipeline");
    let skills: Vec<&str> = card.skills.iter().map(|s| s.id.as_str()).collect();
    assert_eq!(
        skills,
        [
            "start_run",
            "advance",
            "status",
            "evidence",
            "ask",
            "models",
            "agents",
            "registry_change",
            "apply_change",
            "discard_change",
            "approval_document"
        ]
    );
    for name in [
        "status.json",
        "evidence.json",
        "ask.json",
        "models.json",
        "agents.json",
        "change.json",
        "change_refused.json",
        "change_remove.json",
        "applied.json",
        "discarded.json",
        "approval_document.json",
    ] {
        let reply: SendMessageResponse = serde_json::from_value(result(name)).unwrap();
        assert!(matches!(reply, SendMessageResponse::Message(_)), "{name}");
    }
    let task: a2a::Task = serde_json::from_value(result("get_task.json")).unwrap();
    assert_eq!(task.status.state, TaskState::InputRequired);
    assert_eq!(run_id(&task), Some("deploy-design"));
    let tasks: Vec<a2a::Task> =
        serde_json::from_value(result("list_tasks.json")["tasks"].clone()).unwrap();
    assert_eq!(tasks.len(), 1);
    let cancelled: a2a::Task = serde_json::from_value(result("cancel.json")).unwrap();
    assert_eq!(cancelled.status.state, TaskState::Canceled);
}

#[test]
fn a_recorded_run_streams_from_submitted_to_input_required_with_evidence() {
    let events: Vec<StreamResponse> =
        Events::from_reader(std::io::Cursor::new(fixture("start_run.sse")))
            .collect::<Result<_, _>>()
            .unwrap();
    let StreamResponse::Task(first) = &events[0] else {
        panic!("first event is the task")
    };
    assert_eq!(first.status.state, TaskState::Submitted);
    assert_eq!(run_id(first), Some("deploy-design"));
    let entered: Vec<String> = events
        .iter()
        .filter_map(|e| match e {
            StreamResponse::StatusUpdate(u) => u.status.message.as_ref().map(text),
            _ => None,
        })
        .filter(|t| t.starts_with("entered "))
        .collect();
    assert_eq!(entered.first().map(String::as_str), Some("entered G"));
    assert_eq!(entered.last().map(String::as_str), Some("entered H1"));
    let artifact = events.iter().find_map(|e| match e {
        StreamResponse::ArtifactUpdate(a) => Some(&a.artifact),
        _ => None,
    });
    let evidence = data(&artifact.unwrap().parts).unwrap();
    assert!(evidence["survivors"].is_array());
    let StreamResponse::StatusUpdate(last) = events.last().unwrap() else {
        panic!()
    };
    assert_eq!(last.status.state, TaskState::InputRequired);
    assert!(text(last.status.message.as_ref().unwrap()).contains("cannot approve"));
}

#[test]
fn only_a_literal_loopback_address_gets_the_token() {
    for good in [
        "http://127.0.0.1:8420/a2a/jsonrpc",
        "http://[::1]:1/x",
        "http://127.9.9.9:80",
    ] {
        assert!(loopback_origin(good).is_ok(), "{good}");
    }
    for bad in [
        "https://127.0.0.1:8420/a2a/jsonrpc",
        "http://localhost:8420/a2a/jsonrpc",
        "http://10.0.0.1:8420/a2a/jsonrpc",
        "http://0.0.0.0:8420/a2a/jsonrpc",
        "http://127.0.0.1/a2a/jsonrpc",
        "http://example.com:8420/",
    ] {
        assert!(
            matches!(loopback_origin(bad), Err(Error::Config(_))),
            "{bad}"
        );
    }
    assert!(matches!(
        Endpoint::new("http://127.0.0.1:1/x", "short".into()),
        Err(Error::Config(_))
    ));
}

#[cfg(unix)]
#[test]
fn a_token_file_others_can_read_is_refused() {
    use std::os::unix::fs::PermissionsExt;
    let path = token_file(&scratch("exposed-token"));
    std::fs::set_permissions(&path, std::fs::Permissions::from_mode(0o644)).unwrap();
    let refused = Endpoint::from_token_file("http://127.0.0.1:1/x", &path).unwrap_err();
    assert!(
        refused.to_string().contains("readable by others"),
        "{refused}"
    );
}

#[test]
fn a_request_is_one_data_part_naming_one_of_the_eleven_skills() {
    let message = Request::approval_document(
        "r",
        "approve_design_and_no_security_veto",
        "ln-c",
        "ok",
        Some("synthesis_sealed"),
    )
    .message();
    assert!(message.task_id.is_none() && message.context_id.is_none());
    let [part] = message.parts.as_slice() else {
        panic!("one part")
    };
    let PartContent::Data(data) = &part.content else {
        panic!("a data part")
    };
    assert_eq!(
        data,
        &json!({"skill": "approval_document", "run_id": "r",
                "decision": "approve_design_and_no_security_veto", "approver": "ln-c", "reason": "ok",
                "candidate": "synthesis_sealed"})
    );
    let models = Request::models().message();
    let [part] = models.parts.as_slice() else {
        panic!("one part")
    };
    let PartContent::Data(data) = &part.content else {
        panic!("a data part")
    };
    assert_eq!(
        data,
        &json!({"skill": "models"}),
        "the catalog is about the repository: no run id, no arguments"
    );
    assert_eq!(
        Request::ask(Some("r"), "why?").message().parts.len(),
        1,
        "a question is one data part too"
    );
    let skills: Vec<&str> = [
        Request::start_run("r", json!({})),
        Request::advance("r"),
        Request::status("r"),
        Request::evidence("r"),
        Request::ask(None, "why?"),
        Request::models(),
        Request::agents(),
        Request::add_model("p", "m", &[]),
        Request::apply_change("deadbeef"),
        Request::discard_change("deadbeef"),
        Request::approval_document("r", "d", "a", "why", None),
    ]
    .iter()
    .map(Request::skill)
    .collect();
    let card: AgentCard = serde_json::from_str(&fixture("card.json")).unwrap();
    let served: Vec<&str> = card.skills.iter().map(|s| s.id.as_str()).collect();
    assert_eq!(skills, served);
}

fn sent(request: &Request) -> serde_json::Value {
    let message = request.message();
    let [part] = message.parts.as_slice() else {
        panic!("one part")
    };
    let PartContent::Data(data) = &part.content else {
        panic!("a data part")
    };
    data.clone()
}

#[test]
fn registry_changes_name_fields_and_a_confirmation_carries_only_an_id() {
    assert_eq!(
        sent(&Request::add_model(
            "google_ai_pro",
            "gemini-9-new",
            &["design_reasoning".to_string()]
        )),
        json!({"skill": "registry_change", "kind": "add_model", "provider": "google_ai_pro",
               "model_id": "gemini-9-new", "capabilities": ["design_reasoning"]})
    );
    assert_eq!(
        sent(&Request::remove_model("gemini_9_new")),
        json!({"skill": "registry_change", "kind": "remove_model", "model": "gemini_9_new"})
    );
    assert_eq!(
        sent(&Request::bind(
            "design.proposal_b.primary",
            &["gemini_4_argon_high".to_string()]
        )),
        json!({"skill": "registry_change", "kind": "bind", "slot": "design.proposal_b.primary",
               "models": ["gemini_4_argon_high"]})
    );
    // what is written is whatever the server drafted and checked: the request holds no text
    assert_eq!(
        sent(&Request::apply_change("1eb25dbc")),
        json!({"skill": "apply_change", "change_id": "1eb25dbc"})
    );
    assert_eq!(
        sent(&Request::discard_change("1eb25dbc")),
        json!({"skill": "discard_change", "change_id": "1eb25dbc"})
    );
    assert_eq!(sent(&Request::agents()), json!({"skill": "agents"}));
}

#[test]
fn calls_carry_the_token_and_version_and_never_a_task() {
    let fake = Fake::start(|seen| match seen.rpc() {
        "SendMessage" => Reply::json(fixture("status.json")),
        "SendStreamingMessage" => Reply::sse(fixture("start_run.sse")),
        "ListTasks" => Reply::json(fixture("list_tasks.json")),
        _ => Reply::json(r#"{"jsonrpc":"2.0","id":1,"error":{"code":-32601,"message":"no"}}"#),
    });
    let dir = scratch("calls");
    let client = Client::new(Endpoint::from_token_file(&fake.url, &token_file(&dir)).unwrap());
    let SendMessageResponse::Message(reply) =
        client.send(&Request::status("deploy-design")).unwrap()
    else {
        panic!("a message")
    };
    assert_eq!(data(&reply.parts).unwrap()["status"], "awaiting_human");
    let streamed = client
        .stream(&Request::advance("deploy-design"))
        .unwrap()
        .count();
    assert_eq!(
        streamed, 31,
        "the 29 of the run, the progress line and the summary"
    );
    assert_eq!(client.list_tasks().unwrap().len(), 1);
    let refused = client.get_task("x").unwrap_err();
    assert!(
        matches!(refused, Error::Refused { code: -32601, .. }),
        "{refused}"
    );
    for seen in fake.seen() {
        assert_eq!(seen.headers["authorization"], format!("Bearer {TOKEN}"));
        assert_eq!(seen.headers["a2a-version"], "1.0");
        assert!(seen.body.pointer("/params/message/taskId").is_none());
        assert!(seen.body.pointer("/params/message/contextId").is_none());
    }
}

#[test]
fn a_wrong_token_and_an_absent_server_are_reported_not_retried() {
    let fake = Fake::start(|_| Reply {
        status: 401,
        ..Reply::json(r#"{"error":"missing or wrong bearer token"}"#)
    });
    let client = Client::new(Endpoint::new(&fake.url, TOKEN.into()).unwrap());
    let error = client.send(&Request::status("r")).unwrap_err();
    assert!(matches!(error, Error::Http { status: 401, .. }), "{error}");
    assert_eq!(fake.methods(), ["SendMessage"]);

    let closed = std::net::TcpListener::bind("127.0.0.1:0").unwrap();
    let url = format!("http://{}/a2a/jsonrpc", closed.local_addr().unwrap());
    drop(closed);
    let client = Client::new(Endpoint::new(&url, TOKEN.into()).unwrap());
    assert!(matches!(
        client.send(&Request::status("r")),
        Err(Error::Transport(_))
    ));
}

#[test]
fn the_card_must_be_ln_pipelines() {
    let fake = Fake::start(|seen| {
        assert_eq!(
            (seen.method.as_str(), seen.path.as_str()),
            ("GET", "/.well-known/agent-card.json")
        );
        Reply::json(fixture("card.json").replace("\"ln-pipeline\"", "\"someone-else\""))
    });
    let client = Client::new(Endpoint::new(&fake.url, TOKEN.into()).unwrap());
    assert!(matches!(client.card(), Err(Error::Protocol(_))));
}

#[test]
fn every_page_of_tasks_is_listed_without_history() {
    let fake = Fake::start(|seen| {
        let params = &seen.body["params"];
        assert_eq!(params["historyLength"], 0);
        let mut page: Value = serde_json::from_str(&fixture("list_tasks.json")).unwrap();
        match params.get("pageToken").and_then(Value::as_str) {
            None => page["result"]["nextPageToken"] = json!("p2"),
            Some("p2") => {
                page["result"]["tasks"][0]["metadata"]["run_id"] = json!("second-run");
            }
            Some(other) => panic!("unexpected token {other}"),
        }
        Reply::json(page.to_string())
    });
    let client = Client::new(Endpoint::new(&fake.url, TOKEN.into()).unwrap());
    let runs: Vec<String> = client
        .list_tasks()
        .unwrap()
        .iter()
        .map(|t| run_id(t).unwrap().to_string())
        .collect();
    assert_eq!(runs, ["deploy-design", "second-run"]);
    assert_eq!(fake.methods(), ["ListTasks", "ListTasks"]);

    let looping = Fake::start(|_| {
        let mut page: Value = serde_json::from_str(&fixture("list_tasks.json")).unwrap();
        page["result"]["nextPageToken"] = json!("same");
        Reply::json(page.to_string())
    });
    let client = Client::new(Endpoint::new(&looping.url, TOKEN.into()).unwrap());
    assert!(matches!(client.list_tasks(), Err(Error::Protocol(_))));
}
