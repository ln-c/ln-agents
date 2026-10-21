//! A turn as the terminal UI runs it: the real `Prompt` and `TuiHost` against a fake
//! `ln-pipeline` that replays what the real A2A server sent, and the messages applied to an `App`.

#[path = "../../ln-a2a/tests/fake/mod.rs"]
mod fake;

use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::sync::mpsc::{Receiver, channel};
use std::thread;

use agent_client_protocol_schema::v1::SessionUpdate;
use fake::{Fake, Reply, fixture, scratch, token_file};
use ln_acp::Config;
use ln_acp::commands::Mode;
use ln_acp::human::{NoWindow, Pending, Setup};
use ln_acp::turn::{Host, Prompt};
use ln_tui::app::{App, Block, Msg, StageState, ToolState};
use ln_tui::host::{Shared, TuiHost};
use ln_tui::markdown::Theme;

fn replaying() -> Fake {
    Fake::start(
        |seen| match (seen.rpc(), seen.data().and_then(|d| d["skill"].as_str())) {
            ("SendStreamingMessage", _) => Reply::sse(fixture("start_run.sse")),
            ("SendMessage", Some("status")) => Reply::json(fixture("status.json")),
            ("SendMessage", Some("models")) => Reply::json(fixture("models.json")),
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
    let (prompt, _cancel) = Prompt::new(host, dir, None);
    let text = text.to_string();
    let worker = thread::spawn(move || prompt.run_outcome(&text, mode));
    while let Ok(msg) = rx.recv() {
        match msg {
            Msg::Update(update) => app.apply(*update),
            other => app.on_msg(other),
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
