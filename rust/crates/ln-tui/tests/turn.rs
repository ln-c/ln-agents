//! A turn as the terminal UI runs it: the real `Prompt` and `TuiHost` against a fake
//! `ln-pipeline` that replays what the real A2A server sent, and the messages applied to an `App`.

#[path = "../../ln-a2a/tests/fake/mod.rs"]
mod fake;

use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::sync::mpsc::{Receiver, channel};
use std::thread;

use agent_client_protocol_schema::v1::SessionUpdate;
use crossterm::event::{KeyCode, KeyEvent, KeyModifiers};
use fake::{Fake, Reply, fixture, scratch, token_file};
use ln_acp::Config;
use ln_acp::commands::Mode;
use ln_acp::human::{NoWindow, Pending, Setup};
use ln_acp::turn::{Host, Prompt};
use ln_tui::app::{App, Block, Msg, StageState, ToolState};
use ln_tui::host::{Shared, TuiHost};
use ln_tui::markdown::Theme;
use serde_json::{Value, json};

fn replaying() -> Fake {
    Fake::start(
        |seen| match (seen.rpc(), seen.data().and_then(|d| d["skill"].as_str())) {
            ("SendStreamingMessage", _) => Reply::sse(fixture("start_run.sse")),
            ("SendMessage", Some("status")) => Reply::json(fixture("status.json")),
            ("SendMessage", Some("models")) => Reply::json(fixture("models.json")),
            ("SendMessage", Some("evidence")) => Reply::json(fixture("evidence.json")),
            ("SendMessage", Some("approval_document")) => {
                Reply::json(fixture("approval_document.json"))
            }
            _ => Reply::json(fixture("no_run.json")),
        },
    )
}

/// A fake whose `evidence` reply is the recorded approval view after `edit`.
fn offering(edit: impl Fn(&mut Value) + Send + Sync + 'static) -> Fake {
    Fake::start(
        move |seen| match (seen.rpc(), seen.data().and_then(|d| d["skill"].as_str())) {
            ("SendMessage", Some("evidence")) => {
                let mut reply: Value = serde_json::from_str(&fixture("evidence.json")).unwrap();
                edit(&mut reply["result"]["message"]["parts"][0]["data"]);
                Reply::json(reply.to_string())
            }
            ("SendMessage", Some("approval_document")) => {
                Reply::json(fixture("approval_document.json"))
            }
            _ => Reply::json(fixture("no_run.json")),
        },
    )
}

fn host(fake: &Fake, dir: &Path) -> (TuiHost, Receiver<Msg>) {
    let (msg, rx) = channel();
    let config = Config {
        url: fake.url.clone(),
        token_file: Some(token_file(dir)),
        human: Setup::default(),
        window: Arc::new(NoWindow),
    };
    let host = TuiHost {
        config: Arc::new(config),
        cwd: dir.to_path_buf(),
        msg,
        shared: Arc::new(Shared::default()),
    };
    (host, rx)
}

/// Run one turn on a thread, as the UI does, and apply what it sends until the turn is over (the
/// host, and with it the sender, is dropped when the worker ends).
fn turn(app: &mut App, host: TuiHost, rx: &Receiver<Msg>, dir: PathBuf, text: &str, mode: Mode) {
    turn_in(app, host, rx, dir, text, mode, None, &[]);
}

/// [`turn`] in a session already bound to `run`. Each question, pick or line the turn asks is
/// answered with the next of `answers`, typed as keys (`\n` is Enter, `\u{1b}` is Esc); one left
/// unanswered would hold the turn for ever.
#[allow(clippy::too_many_arguments)]
fn turn_in(
    app: &mut App,
    host: TuiHost,
    rx: &Receiver<Msg>,
    dir: PathBuf,
    text: &str,
    mode: Mode,
    run: Option<&str>,
    answers: &[&str],
) {
    let (prompt, _cancel) = Prompt::new(host, dir, run.map(str::to_string));
    let text = text.to_string();
    let worker = thread::spawn(move || prompt.run_outcome(&text, mode));
    let mut answers = answers.iter();
    while let Ok(msg) = rx.recv() {
        match msg {
            Msg::Update(update) => app.apply(*update),
            other => {
                app.on_msg(other);
                if app.question.is_some() || app.pick.is_some() || app.line.is_some() {
                    let keys = answers.next().expect("an answer for what the turn asked");
                    for c in keys.chars() {
                        let code = match c {
                            '\n' => KeyCode::Enter,
                            '\u{1b}' => KeyCode::Esc,
                            c => KeyCode::Char(c),
                        };
                        app.on_key(KeyEvent::new(code, KeyModifiers::NONE));
                    }
                }
            }
        }
    }
    worker.join().expect("the turn does not panic");
}

fn texts(app: &mut App) -> String {
    app.transcript(100)
        .iter()
        .map(|l| {
            l.spans
                .iter()
                .map(|s| s.content.as_ref())
                .collect::<String>()
        })
        .collect::<Vec<_>>()
        .join("\n")
}

#[test]
fn a_run_advancing_from_grounding_to_the_design_gate_fills_the_stages_and_the_transcript() {
    let fake = replaying();
    let dir = scratch("tui-advance");
    let (host, rx) = host(&fake, &dir);
    let mut app = App::new(Mode::Design, Theme::new(false));
    turn(&mut app, host, &rx, dir, "/advance r", Mode::Design);

    let names: Vec<&str> = app.stages.iter().map(|s| s.name.as_str()).collect();
    assert_eq!(
        names,
        [
            "G", "A1", "A2", "A2J", "A3", "A4", "A3R", "B1", "B2", "B3", "H1"
        ]
    );
    assert!(
        app.stages[..10].iter().all(|s| s.state == StageState::Done),
        "{:?}",
        app.stages
    );
    assert_eq!(app.stages[10].state, StageState::Working);
    assert_eq!(app.run.as_deref(), Some("r"), "the run the command named");

    let calls: Vec<ToolState> = app
        .blocks()
        .filter_map(|b| match b {
            Block::Tool { state, .. } => Some(*state),
            _ => None,
        })
        .collect();
    assert_eq!(calls.len(), 7, "seven model calls");
    assert!(calls.iter().all(|s| *s == ToolState::Done), "{calls:?}");

    let text = texts(&mut app);
    assert!(text.contains("advancing"), "{text}");
    assert!(text.contains("Stopped, needs a human"), "{text}");
    assert!(text.contains("A1.proposal_a"), "{text}");
}

#[test]
fn a_command_that_fails_is_a_message_and_the_turn_still_ends() {
    let fake = replaying();
    let dir = scratch("tui-failure");
    let (host, rx) = host(&fake, &dir);
    let mut app = App::new(Mode::Status, Theme::new(false));
    // no run is bound, so `/evidence` has nothing to ask about
    turn(&mut app, host, &rx, dir, "/evidence", Mode::Status);
    assert!(texts(&mut app).contains("No run in this session"));
    assert!(fake.seen().is_empty());
}

#[test]
fn the_write_permission_is_asked_on_the_ui_thread_and_defaults_to_no() {
    let fake = replaying();
    let dir = scratch("tui-ask");
    let (asker, rx) = host(&fake, &dir);
    let asking = thread::spawn(move || asker.allow_write("r1", "approve"));
    let Msg::Ask { title, reply } = rx.recv().expect("a question") else {
        panic!("a question first");
    };
    assert!(
        title.contains("unsigned approve document for run r1"),
        "{title}"
    );
    reply.send(true).unwrap();
    assert!(asking.join().unwrap());

    // the UI going away (its receiver dropped) is a no, not a yes
    let (listenerless, rx) = host(&fake, &dir);
    drop(rx);
    assert!(!listenerless.allow_write("r1", "approve"));
}

#[test]
fn refusing_the_write_says_so_once_and_sends_nothing() {
    let fake = replaying();
    let dir = scratch("tui-refuse");
    let (host, rx) = host(&fake, &dir);
    let mut app = App::new(Mode::Design, Theme::new(false));
    turn_in(
        &mut app,
        host,
        &rx,
        dir,
        "/approval-doc approve me trying",
        Mode::Design,
        Some("r"),
        &["\n"],
    );
    let text = texts(&mut app);
    assert_eq!(text.matches("Not written.").count(), 1, "{text}");
    assert!(
        fake.seen().is_empty(),
        "a refused write asks the server nothing"
    );
}

#[test]
fn a_step_for_the_human_runs_in_this_terminal_and_the_host_says_how_it_went() {
    let fake = replaying();
    let dir = scratch("tui-foreground");
    let (host, rx) = host(&fake, &dir);
    let args = ["decide".to_string(), "r1".to_string()];
    for (result, expected) in [
        (Ok(0), "ended with status 0"),
        (Ok(3), "did not complete"),
        (
            Err("no such program".to_string()),
            "could not start: no such program",
        ),
    ] {
        let sent = args.clone();
        thread::scope(|scope| {
            scope.spawn(|| host.hand_over(&sent, "r1", "sign", "ln-agents decide r1"));
            let Msg::Foreground { args, reply, .. } = rx.recv().expect("a step") else {
                panic!("a step first");
            };
            assert_eq!(args, sent);
            reply.send(result).unwrap();
            let Msg::Update(update) = rx.recv().expect("what happened") else {
                panic!("an update");
            };
            let SessionUpdate::AgentMessageChunk(chunk) = *update else {
                panic!("a message");
            };
            let agent_client_protocol_schema::v1::ContentBlock::Text(text) = chunk.content else {
                panic!("text");
            };
            assert!(text.text.contains(expected), "{}", text.text);
        });
    }
}

#[test]
fn the_unsigned_document_is_kept_for_the_next_turn() {
    let fake = replaying();
    let dir = scratch("tui-pending");
    let (host, _rx) = host(&fake, &dir);
    assert!(host.pending().is_none());
    let pending = Pending {
        run: "r1".into(),
        document: dir.join("approvals/H1.json"),
        namespace: "ns".into(),
        sign: "sign".into(),
        then: "then".into(),
    };
    host.remember(pending.clone());
    assert_eq!(host.pending(), Some(pending));
}

/// The `approval_document` request the fake was sent, if any.
fn written(fake: &Fake) -> Option<Value> {
    fake.seen()
        .iter()
        .filter(|s| s.data().is_some_and(|d| d["skill"] == "approval_document"))
        .map(|s| s.data().unwrap().clone())
        .next_back()
}

fn pick_flow(fake: &Fake, name: &str, answers: &[&str]) -> (App, String) {
    let dir = scratch(&format!("tui-pick-{name}"));
    let (host, rx) = host(fake, &dir);
    let mut app = App::new(Mode::Design, Theme::new(false));
    turn_in(
        &mut app,
        host,
        &rx,
        dir,
        "/approval-doc",
        Mode::Design,
        Some("deploy-design"),
        answers,
    );
    let text = texts(&mut app);
    (app, text)
}

#[test]
fn a_pick_and_a_reason_write_the_document_the_orchestrator_listed() {
    let fake = offering(|_| {});
    let (_app, text) = pick_flow(&fake, "writes", &["2", "the second is the simpler one\n"]);
    let sent = written(&fake).unwrap_or_else(|| panic!("the document was asked for: {text}"));
    assert_eq!(sent["decision"], "approve_design_and_no_security_veto");
    assert_eq!(
        sent["candidate"], "proposal_b_sealed",
        "option 2 is Design B"
    );
    assert_eq!(
        sent["approver"], "ln-c",
        "the policy names one approver, so no pick"
    );
    assert_eq!(sent["reason"], "the second is the simpler one");
    assert_eq!(sent["run_id"], "deploy-design");
    assert!(sent.get("remediation_cause").is_none());
    assert!(
        text.contains("Approve Design B: the second is the simpler one"),
        "the choice and the reason are echoed: {text}"
    );
    assert!(text.contains("Wrote the unsigned"), "{text}");
    assert!(text.contains("/decide"), "{text}");
    assert!(!text.contains("cannot use this document"), "{text}");
    assert_eq!(
        fake.methods(),
        ["SendMessage", "SendMessage"],
        "the choices, then the document: no write was asked of the person"
    );
}

#[test]
fn the_picker_offers_exactly_what_the_orchestrator_listed_and_a_report_to_read() {
    let fake = offering(|_| {});
    let dir = scratch("tui-offer");
    let (host, rx) = host(&fake, &dir);
    let asking = thread::spawn(move || {
        host.choose(
            "t",
            &["Approve Design A".to_string(), "Reject".to_string()],
            Some("# Report"),
        )
    });
    let Msg::Pick {
        title,
        options,
        report,
        reply,
    } = rx.recv().expect("a pick")
    else {
        panic!("a pick first");
    };
    assert_eq!(title, "t");
    assert_eq!(options, ["Approve Design A", "Reject"]);
    assert_eq!(report.as_deref(), Some("# Report"));
    reply.send(Some(1)).unwrap();
    assert_eq!(asking.join().unwrap(), Some(1));
}

#[test]
fn the_real_h1_choices_are_all_on_offer_in_the_order_the_orchestrator_gave_them() {
    let fake = offering(|_| {});
    let dir = scratch("tui-h1");
    let (host, rx) = host(&fake, &dir);
    let mut app = App::new(Mode::Design, Theme::new(false));
    let (prompt, _cancel) = Prompt::new(host, dir, Some("deploy-design".into()));
    let worker = thread::spawn(move || prompt.run_outcome("/approval-doc", Mode::Design));
    let mut offered = Vec::new();
    while let Ok(msg) = rx.recv() {
        if let Msg::Pick {
            options, report, ..
        } = &msg
        {
            offered = options.clone();
            assert!(
                report
                    .as_deref()
                    .is_some_and(|r| r.contains("## Your choices"))
            );
        }
        app.on_msg(msg);
        if app.pick.is_some() {
            app.on_key(KeyEvent::new(KeyCode::Esc, KeyModifiers::NONE));
        }
    }
    worker.join().unwrap();
    assert_eq!(
        offered,
        [
            "Approve Design A",
            "Approve Design B",
            "Approve the synthesis",
            "Request evidence",
            "Revise with notes",
            "Approve remediation or constraint change",
            "Reject",
            "Halt"
        ]
    );
    assert!(written(&fake).is_none());
}

#[test]
fn leaving_the_pick_or_the_reason_writes_nothing() {
    for answers in [&["\u{1b}"][..], &["1", "\u{1b}"][..]] {
        let fake = offering(|_| {});
        let (app, text) = pick_flow(&fake, &format!("leaves-{}", answers.len()), answers);
        assert!(text.contains("Nothing was written."), "{text}");
        assert!(written(&fake).is_none());
        assert!(app.pick.is_none() && app.line.is_none());
    }
}

#[test]
fn several_approvers_are_picked_too() {
    let fake = offering(|view| view["approvers"] = json!(["ln-c", "ln-d"]));
    let (_app, text) = pick_flow(&fake, "approvers", &["1", "2", "fine\n"]);
    let sent = written(&fake).expect("the document was asked for");
    assert_eq!(sent["approver"], "ln-d");
    assert_eq!(sent["candidate"], "proposal_a_sealed");
    assert!(text.contains("Wrote the unsigned"), "{text}");
}

#[test]
fn a_policy_with_no_approver_is_a_failure_not_a_default() {
    let fake = offering(|view| view["approvers"] = json!([]));
    let (_app, text) = pick_flow(&fake, "no-approver", &["1"]);
    assert!(text.contains("names no approver"), "{text}");
    assert!(written(&fake).is_none());
}

#[test]
fn a_merge_denial_carries_its_cause() {
    let fake = offering(|view| {
        view["node"] = json!("R1");
        view["choices"] = json!([
            {"decision": "approve", "label": "Approve", "candidate": null, "remediation_cause": null},
            {"decision": "deny", "label": "Deny: test defect", "candidate": null,
             "remediation_cause": "test_defect"}
        ]);
    });
    let (_app, _text) = pick_flow(&fake, "denial", &["2", "the tests prove nothing\n"]);
    let sent = written(&fake).expect("the document was asked for");
    assert_eq!(sent["decision"], "deny");
    assert_eq!(sent["remediation_cause"], "test_defect");
    assert!(sent.get("candidate").is_none());
}

#[test]
fn an_orchestrator_that_lists_no_choices_says_how_to_type_the_command() {
    let fake = offering(|view| {
        view.as_object_mut().unwrap().remove("choices");
    });
    let (_app, text) = pick_flow(&fake, "no-choices", &[]);
    assert!(text.contains("does not list the choices"), "{text}");
    assert!(text.contains("<decision>"), "{text}");
}
