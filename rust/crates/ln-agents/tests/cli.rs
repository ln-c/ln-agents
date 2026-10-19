use std::process::Command;

fn ln_agents(args: &[&str]) -> std::process::Output {
    Command::new(env!("CARGO_BIN_EXE_ln-agents"))
        .args(args)
        .output()
        .expect("ln-agents runs")
}

#[test]
fn version_names_the_binary_and_the_workspace_version() {
    let out = ln_agents(&["--version"]);
    assert!(out.status.success());
    let text = String::from_utf8(out.stdout).expect("utf-8");
    assert_eq!(
        text.trim(),
        format!("ln-agents {}", env!("CARGO_PKG_VERSION"))
    );
}

#[test]
fn an_unknown_argument_is_refused() {
    let out = ln_agents(&["--no-such-flag"]);
    assert!(!out.status.success());
}
