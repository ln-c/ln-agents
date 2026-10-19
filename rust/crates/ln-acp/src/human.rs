//! The two things only a human does, run in a terminal window the human is looking at:
//! `/decide` signs an approval document with the human's SSH key and records the decision,
//! `/resume` records that a paused run may go on. This crate never starts a process: it only
//! works out the words of the window ([`decide_args`], [`resume_args`]) and hands them to a
//! [`Window`], which the `ln-agents` binary implements. What runs in the window shows what is at
//! stake, asks the human to type the run id, and lets `ssh-keygen` ask for the passphrase on that
//! terminal; the orchestrator still verifies the signature against `policy/approvers.yaml` and
//! takes the edge itself.
//!
//! Only the slash commands reach this module, never a model's output or a question put to the
//! coordinator.

use std::path::{Path, PathBuf};
use std::sync::Arc;

use serde_json::Value;

/// What the window runs to reach the orchestrator when `LN_AGENTS_PIPELINE` names nothing else.
pub const DEFAULT_PIPELINE: [&str; 5] = ["uv", "run", "python", "-m", "agentic_pipeline"];
/// Where a session's runs live relative to its cwd when `LN_AGENTS_RUNS_DIR` names nothing else.
pub const DEFAULT_RUNS_DIR: &str = ".runs";

/// Terminal emulators ln-agents can open, with the words that put the command to run after them.
const KNOWN: [(&str, &[&str]); 10] = [
    ("xdg-terminal-exec", &[]),
    ("foot", &[]),
    ("kitty", &[]),
    ("alacritty", &["-e"]),
    ("wezterm", &["start", "--"]),
    ("ghostty", &["-e"]),
    ("gnome-terminal", &["--"]),
    ("konsole", &["-e"]),
    ("xfce4-terminal", &["-x"]),
    ("xterm", &["-e"]),
];

/// Opens a terminal window running `ln-agents <args>` in `cwd` for the human; the command line it
/// ran, or why there is no window. Implemented by the binary, which alone may start a process.
pub trait Window: Send + Sync + std::fmt::Debug {
    fn open(&self, cwd: &Path, args: &[String]) -> Result<String, String>;
}

/// A window that is never available, for a library user that has no binary.
#[derive(Debug)]
pub struct NoWindow;

impl Window for NoWindow {
    fn open(&self, _cwd: &Path, _args: &[String]) -> Result<String, String> {
        Err("this ln-agents cannot open a terminal window".to_string())
    }
}

/// The machine's settings for these two commands (environment variables or flags of
/// `ln-agents acp`); every field has a default.
#[derive(Debug, Clone, Default)]
pub struct Setup {
    /// `LN_AGENTS_TERMINAL`: the words to put before the command to run, such as `kitty` or
    /// `alacritty -e`. Unset: `$TERMINAL`, then `xdg-terminal-exec`, then a known emulator.
    pub terminal: Option<String>,
    /// `LN_AGENTS_PIPELINE`: how the window runs the orchestrator's CLI, split on spaces.
    pub pipeline: Vec<String>,
    /// `LN_AGENTS_SSH_KEY`: the private key the human signs with.
    pub key: Option<PathBuf>,
    /// `LN_AGENTS_RUNS_DIR`: where `/resume` finds runs; `<session cwd>/.runs` when unset.
    pub runs_dir: Option<PathBuf>,
}

impl Setup {
    fn pipeline(&self) -> Vec<String> {
        if self.pipeline.is_empty() {
            DEFAULT_PIPELINE.iter().map(|w| (*w).to_string()).collect()
        } else {
            self.pipeline.clone()
        }
    }
}

/// The unsigned document `/approval-doc` wrote, as the orchestrator described it.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Pending {
    pub run: String,
    pub document: PathBuf,
    pub namespace: String,
    /// The commands a human can run by hand instead, as the orchestrator printed them.
    pub sign: String,
    pub then: String,
}

impl Pending {
    pub fn from_reply(run: &str, reply: &Value) -> Result<Pending, String> {
        let field = |key: &str| {
            reply
                .get(key)
                .and_then(Value::as_str)
                .map(str::to_string)
                .ok_or_else(|| {
                    format!(
                        "the orchestrator's reply has no `{key}`; restart `serve a2a` with the \
                         same version as ln-agents"
                    )
                })
        };
        Ok(Pending {
            run: run.to_string(),
            document: PathBuf::from(field("document")?),
            namespace: field("namespace")?,
            sign: field("sign")?,
            then: field("then")?,
        })
    }
}

/// `<runs>/<run>/approvals/<file>`: the runs directory a document sits in, if it sits in one.
pub fn runs_dir_of(document: &Path, run: &str) -> Result<PathBuf, String> {
    let approvals = document.parent();
    let run_dir = approvals.and_then(Path::parent);
    let named = |dir: Option<&Path>, name: &str| {
        dir.and_then(Path::file_name)
            .is_some_and(|n| n.to_str() == Some(name))
    };
    match (named(approvals, "approvals"), named(run_dir, run)) {
        (true, true) => run_dir
            .and_then(Path::parent)
            .map(Path::to_path_buf)
            .ok_or_else(|| format!("{} has no runs directory", document.display())),
        _ => Err(format!(
            "{} is not <runs>/{run}/approvals/<file>, so it is not a document of run {run}",
            document.display()
        )),
    }
}

/// The words after `ln-agents` that make up the window for `/decide`.
pub fn decide_args(
    setup: &Setup,
    run: &str,
    document: &Path,
    namespace: &str,
) -> Result<Vec<String>, String> {
    let runs_dir = runs_dir_of(document, run)?;
    let mut args = vec![
        "decide".to_string(),
        run.to_string(),
        "--approval".to_string(),
        document.display().to_string(),
        "--namespace".to_string(),
        namespace.to_string(),
        "--runs-dir".to_string(),
        runs_dir.display().to_string(),
    ];
    if let Some(key) = &setup.key {
        args.extend(["--key".to_string(), key.display().to_string()]);
    }
    args.push("--".to_string());
    args.extend(setup.pipeline());
    Ok(args)
}

/// The words after `ln-agents` that make up the window for `/resume`.
pub fn resume_args(setup: &Setup, cwd: &Path, run: &str, by: &str, reason: &str) -> Vec<String> {
    let runs_dir = setup
        .runs_dir
        .clone()
        .unwrap_or_else(|| cwd.join(DEFAULT_RUNS_DIR));
    let mut args = vec![
        "resume".to_string(),
        run.to_string(),
        "--by".to_string(),
        by.to_string(),
        "--reason".to_string(),
        reason.to_string(),
        "--runs-dir".to_string(),
        runs_dir.display().to_string(),
        "--".to_string(),
    ];
    args.extend(setup.pipeline());
    args
}

/// The words that put a command in a new terminal window, or why there are none. `terminal_env`
/// is `$TERMINAL`; `on_path` says whether a program can be found.
pub fn terminal_words(
    setup: &Setup,
    terminal_env: Option<&str>,
    on_path: &dyn Fn(&str) -> bool,
) -> Result<Vec<String>, String> {
    let words = |text: &str| -> Vec<String> { text.split_whitespace().map(String::from).collect() };
    let flags_of = |program: &str| -> Vec<String> {
        let name = Path::new(program)
            .file_name()
            .and_then(|n| n.to_str())
            .unwrap_or(program);
        let flags: &[&str] = KNOWN
            .iter()
            .find(|(known, _)| *known == name)
            .map_or(&["-e"], |(_, flags)| flags);
        flags.iter().map(|f| (*f).to_string()).collect()
    };
    if let Some(own) = setup
        .terminal
        .as_deref()
        .map(words)
        .filter(|w| !w.is_empty())
    {
        return Ok(own);
    }
    if let Some(program) = terminal_env.map(words).and_then(|w| w.into_iter().next()) {
        let mut found = flags_of(&program);
        found.insert(0, program);
        return Ok(found);
    }
    KNOWN
        .iter()
        .find(|(name, _)| on_path(name))
        .map(|(name, flags)| {
            let mut found = vec![(*name).to_string()];
            found.extend(flags.iter().map(|f| (*f).to_string()));
            found
        })
        .ok_or_else(|| {
            "no terminal emulator was found; set LN_AGENTS_TERMINAL to the words that open one, \
             for example `kitty` or `alacritty -e`"
                .to_string()
        })
}

/// A command line for people to read; quoting only where a word needs it.
pub fn display(words: &[String]) -> String {
    words
        .iter()
        .map(|w| {
            if !w.is_empty()
                && w.chars()
                    .all(|c| c.is_alphanumeric() || "-_./:=@%+,".contains(c))
            {
                w.clone()
            } else {
                format!("'{}'", w.replace('\'', r"'\''"))
            }
        })
        .collect::<Vec<_>>()
        .join(" ")
}

/// What `ln-agents acp` hands to every session: how to open the human's windows.
pub type SharedWindow = Arc<dyn Window>;

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn a_document_names_the_runs_directory_it_sits_in() {
        let doc = Path::new("/work/runs/r1/approvals/H1-x.json");
        assert_eq!(runs_dir_of(doc, "r1"), Ok(PathBuf::from("/work/runs")));
        assert!(runs_dir_of(doc, "r2").is_err(), "another run's document");
        assert!(runs_dir_of(Path::new("/tmp/H1-x.json"), "r1").is_err());
        assert!(runs_dir_of(Path::new("/work/runs/r1/other/H1-x.json"), "r1").is_err());
    }

    #[test]
    fn the_terminal_is_the_override_then_terminal_then_a_known_emulator() {
        let none = |_: &str| false;
        let only = |name: &'static str| move |p: &str| p == name;
        let over = Setup {
            terminal: Some("alacritty --class ln -e".into()),
            ..Setup::default()
        };
        assert_eq!(
            terminal_words(&over, Some("kitty"), &none).unwrap(),
            ["alacritty", "--class", "ln", "-e"]
        );
        let plain = Setup::default();
        assert_eq!(
            terminal_words(&plain, Some("/usr/bin/alacritty"), &none).unwrap(),
            ["/usr/bin/alacritty", "-e"]
        );
        assert_eq!(
            terminal_words(&plain, Some("foot"), &none).unwrap(),
            ["foot"]
        );
        assert_eq!(
            terminal_words(&plain, Some("some-unknown"), &none).unwrap(),
            ["some-unknown", "-e"]
        );
        assert_eq!(
            terminal_words(&plain, None, &only("xdg-terminal-exec")).unwrap(),
            ["xdg-terminal-exec"]
        );
        assert_eq!(
            terminal_words(&plain, None, &only("gnome-terminal")).unwrap(),
            ["gnome-terminal", "--"]
        );
        let error = terminal_words(&plain, None, &none).unwrap_err();
        assert!(error.contains("LN_AGENTS_TERMINAL"), "{error}");
        let blank = Setup {
            terminal: Some("  ".into()),
            ..Setup::default()
        };
        assert!(
            terminal_words(&blank, None, &none).is_err(),
            "blank is unset"
        );
    }

    #[test]
    fn the_window_gets_the_arguments_as_a_list() {
        let setup = Setup {
            key: Some(PathBuf::from("/home/me/my key")),
            ..Setup::default()
        };
        let doc = Path::new("/work/runs/r1/approvals/H1-x.json");
        let args = decide_args(&setup, "r1", doc, "ns").unwrap();
        assert_eq!(
            args,
            [
                "decide",
                "r1",
                "--approval",
                "/work/runs/r1/approvals/H1-x.json",
                "--namespace",
                "ns",
                "--runs-dir",
                "/work/runs",
                "--key",
                "/home/me/my key",
                "--",
                "uv",
                "run",
                "python",
                "-m",
                "agentic_pipeline",
            ]
        );
        let args = resume_args(
            &Setup::default(),
            Path::new("/proj"),
            "r1",
            "ln-c",
            "quota is back",
        );
        assert_eq!(
            args,
            [
                "resume",
                "r1",
                "--by",
                "ln-c",
                "--reason",
                "quota is back",
                "--runs-dir",
                Path::new("/proj").join(".runs").to_str().unwrap(),
                "--",
                "uv",
                "run",
                "python",
                "-m",
                "agentic_pipeline",
            ]
        );
        assert_eq!(
            display(&["a b".into(), "it's".into(), "ok".into()]),
            r"'a b' 'it'\''s' ok"
        );
    }

    #[test]
    fn the_reply_of_approval_document_becomes_a_pending_document() {
        let reply = json!({"document": "/w/runs/r1/approvals/H1-a.json", "namespace": "ns",
                           "sign": "ssh-keygen ...", "then": "python -m ..."});
        let pending = Pending::from_reply("r1", &reply).unwrap();
        assert_eq!(
            pending.document,
            Path::new("/w/runs/r1/approvals/H1-a.json")
        );
        assert_eq!(pending.namespace, "ns");
        let old = json!({"document": "/x", "sign": "s", "then": "t"});
        let error = Pending::from_reply("r1", &old).unwrap_err();
        assert!(
            error.contains("`namespace`") && error.contains("serve a2a"),
            "{error}"
        );
    }
}
