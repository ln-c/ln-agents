use std::process::Command;

fn ln_hub(args: &[&str]) -> std::process::Output {
    Command::new(env!("CARGO_BIN_EXE_ln-hub"))
        .args(args)
        .output()
        .expect("ln-hub runs")
}

#[test]
fn version_names_the_binary_and_the_workspace_version() {
    let out = ln_hub(&["--version"]);
    assert!(out.status.success());
    let text = String::from_utf8(out.stdout).expect("utf-8");
    assert_eq!(text.trim(), format!("ln-hub {}", env!("CARGO_PKG_VERSION")));
}

#[test]
fn an_unknown_argument_is_refused() {
    let out = ln_hub(&["--no-such-flag"]);
    assert!(!out.status.success());
}
