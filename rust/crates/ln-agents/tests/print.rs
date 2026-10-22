//! `ln-agents -p` as a script sees it: one prompt in, the answer on stdout, stages and errors on
//! stderr, against a fake `ln-pipeline` that replays what the real A2A server sent.

#[path = "../../ln-a2a/tests/fake/mod.rs"]
mod fake;

use std::path::Path;
use std::process::{Command, Output};

use fake::{Fake, Reply, fixture, scratch, token_file};
use serde_json::{Value, json};

fn replaying() -> Fake {
    Fake::start(
        |seen| match (seen.rpc(), seen.data().and_then(|d| d["skill"].as_str())) {
            ("SendStreamingMessage", _) => Reply::sse(fixture("start_run.sse")),
            ("SendMessage", Some("status")) => Reply::json(fixture("status.json")),
            ("SendMessage", Some("models")) => Reply::json(fixture("models.json")),
            ("SendMessage", Some("complete_mapping")) => {
                Reply::json(fixture("complete_mapping_none.json"))
            }
            ("SendMessage", Some("approval_document")) => {
                Reply::json(fixture("approval_document.json"))
            }
            _ => Reply::json(fixture("no_run.json")),
        },
    )
}

fn skills_sent(fake: &Fake) -> Vec<Value> {
    fake.seen()
        .iter()
        .filter_map(|s| s.data().cloned())
        .collect()
}

fn print(url: &str, token: &Path, args: &[&str], stdin: Option<&str>) -> Output {
    use std::io::Write;
    use std::process::Stdio;
    let mut child = Command::new(env!("CARGO_BIN_EXE_ln-agents"))
        .args(["--a2a-url", url, "--token-file"])
        .arg(token)
        .args(args)
        .env_remove("LN_AGENTS_A2A_URL")
        .env_remove("LN_AGENTS_TOKEN_FILE")
        .stdin(if stdin.is_some() {
            Stdio::piped()
        } else {
            Stdio::null()
        })
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .spawn()
        .expect("ln-agents starts");
    if let Some(text) = stdin {
        child
            .stdin
            .take()
            .expect("stdin")
            .write_all(text.as_bytes())
            .expect("write the prompt");
    }
    child.wait_with_output().expect("ln-agents runs")
}

fn text(bytes: &[u8]) -> String {
    String::from_utf8(bytes.to_vec()).expect("utf-8")
}

#[test]
fn a_command_prints_its_markdown_on_stdout_and_nothing_else() {
    let fake = replaying();
    let dir = scratch("print-models");
    let out = print(&fake.url, &token_file(&dir), &["-p", "/models"], None);
    assert!(out.status.success(), "{}", text(&out.stderr));
    let stdout = text(&out.stdout);
    assert!(
        stdout.contains("| `qwen3_5_9b` | llamacpp_local:qwen3.5-9b | qwen3_5 | local |"),
        "{stdout}"
    );
    assert_eq!(text(&out.stderr), "");
    assert_eq!(skills_sent(&fake), [json!({"skill": "models"})]);
}

#[test]
fn the_prompt_can_come_from_stdin() {
    let fake = replaying();
    let dir = scratch("print-stdin");
    let out = print(&fake.url, &token_file(&dir), &["-p"], Some("/models\n"));
    assert!(out.status.success(), "{}", text(&out.stderr));
    assert!(text(&out.stdout).contains("Known, unclaimed"));
    assert_eq!(skills_sent(&fake), [json!({"skill": "models"})]);
}

#[test]
fn status_mode_is_read_only_and_a_refusal_goes_to_stderr_with_status_1() {
    let fake = replaying();
    let dir = scratch("print-readonly");
    for prompt in [
        "/advance r",
        "/start r spec.json",
        "/decide",
        "/resume ln-c because",
    ] {
        let out = print(&fake.url, &token_file(&dir), &["-p", prompt], None);
        assert_eq!(out.status.code(), Some(1), "{prompt}");
        assert_eq!(text(&out.stdout), "", "{prompt}");
        assert!(text(&out.stderr).contains("read-only"), "{prompt}");
    }
    assert!(fake.seen().is_empty(), "nothing reached the orchestrator");
}

#[test]
fn design_mode_advances_a_run_and_reports_its_stages_on_stderr() {
    let fake = replaying();
    let dir = scratch("print-advance");
    let out = print(
        &fake.url,
        &token_file(&dir),
        &["--mode", "design", "-p", "/advance r"],
        None,
    );
    assert!(out.status.success(), "{}", text(&out.stderr));
    let stdout = text(&out.stdout);
    assert!(stdout.contains("advancing"), "{stdout}");
    assert!(
        !stdout.contains("[stage]"),
        "stages are not the answer: {stdout}"
    );
    assert!(
        text(&out.stderr).contains("[stage] "),
        "{}",
        text(&out.stderr)
    );
}

#[test]
fn an_approval_document_is_never_written_without_a_person() {
    let fake = replaying();
    let dir = scratch("print-no-write");
    let out = print(
        &fake.url,
        &token_file(&dir),
        &["--mode", "design", "-p", "/advance r"],
        None,
    );
    assert!(out.status.success());
    // /approval-doc asks to write, and -p answers no; the run is bound only inside one process,
    // so a second -p has no run to write for.
    let out = print(
        &fake.url,
        &token_file(&dir),
        &["--mode", "design", "-p", "/approval-doc d ln-c because"],
        None,
    );
    assert_eq!(out.status.code(), Some(1));
    assert!(text(&out.stderr).contains("No run in this session"));
    assert!(
        !skills_sent(&fake)
            .iter()
            .any(|s| s["skill"] == "approval_document")
    );
}

#[test]
fn an_orchestrator_that_is_not_running_is_an_error_on_stderr() {
    let dir = scratch("print-down");
    let token = token_file(&dir);
    let out = print(
        "http://127.0.0.1:9/a2a/jsonrpc",
        &token,
        &["-p", "/models"],
        None,
    );
    assert_eq!(out.status.code(), Some(1));
    assert_eq!(text(&out.stdout), "");
    assert!(
        text(&out.stderr).contains("orchestrator"),
        "{}",
        text(&out.stderr)
    );
}

#[test]
fn a_remote_endpoint_is_refused_before_anything_is_sent() {
    let dir = scratch("print-remote");
    let out = print(
        "http://192.0.2.1:8420/a2a/jsonrpc",
        &token_file(&dir),
        &["-p", "/models"],
        None,
    );
    assert_eq!(out.status.code(), Some(1));
    assert!(
        text(&out.stderr).contains("loopback"),
        "{}",
        text(&out.stderr)
    );
}

#[test]
fn a_flag_before_a_subcommand_is_refused_not_ignored() {
    let out = Command::new(env!("CARGO_BIN_EXE_ln-agents"))
        .args(["--token-file", "/nonexistent", "acp"])
        .stdin(std::process::Stdio::null())
        .output()
        .expect("ln-agents runs");
    assert_eq!(out.status.code(), Some(2));
    assert!(
        text(&out.stderr).contains("--token-file before a subcommand"),
        "{}",
        text(&out.stderr)
    );
}

#[test]
fn the_environment_and_flags_after_a_subcommand_still_configure_acp() {
    // standard input closes at once, so `acp` serves nothing and ends cleanly
    for (args, env) in [
        (
            vec!["acp", "--a2a-url", "http://127.0.0.1:1/a2a/jsonrpc"],
            None,
        ),
        (vec!["acp"], Some("http://127.0.0.1:1/a2a/jsonrpc")),
    ] {
        let mut command = Command::new(env!("CARGO_BIN_EXE_ln-agents"));
        command.args(&args).stdin(std::process::Stdio::null());
        if let Some(url) = env {
            command.env("LN_AGENTS_A2A_URL", url);
        }
        let out = command.output().expect("ln-agents runs");
        assert_eq!(out.status.code(), Some(0), "{args:?} {env:?}");
    }
}

#[test]
fn a_prompt_with_a_subcommand_is_a_usage_error() {
    let out = Command::new(env!("CARGO_BIN_EXE_ln-agents"))
        .args(["-p", "/models", "acp"])
        .output()
        .expect("ln-agents runs");
    assert_eq!(out.status.code(), Some(2));
}

#[test]
fn bare_ln_agents_without_a_terminal_says_so_and_points_at_dash_p() {
    let out = Command::new(env!("CARGO_BIN_EXE_ln-agents"))
        .stdin(std::process::Stdio::null())
        .output()
        .expect("ln-agents runs");
    assert_eq!(out.status.code(), Some(1));
    let stderr = text(&out.stderr);
    assert!(
        stderr.contains("needs a terminal") && stderr.contains("-p"),
        "{stderr}"
    );
}
