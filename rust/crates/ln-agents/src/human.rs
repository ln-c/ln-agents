//! The two things only a human does, run in a terminal the human is looking at:
//! `/decide` signs an approval document with the human's SSH key and records the decision,
//! `/resume` records that a paused run may go on. `ln-agents acp` opens a window for it
//! ([`Terminal`]); `ln-agents` itself runs it in the terminal it is already in ([`Here`]), after
//! its screen is given back. What runs there ([`decide`], [`resume`]) shows what is at stake, asks
//! the human to type the run id, and lets `ssh-keygen` ask for the passphrase on that terminal.
//! The orchestrator still verifies the signature against `policy/approvers.yaml` and takes the
//! edge itself.
//!
//! This is the only place in the ln-agents workspace that starts a process (`ln-acp` and `ln-a2a`
//! never do), and only the slash commands reach it, never a model's output.

use std::io::{self, BufRead, IsTerminal, Write};
use std::path::{Path, PathBuf};
use std::process::{Command, Stdio};
use std::time::{Duration, Instant};

use ln_acp::human::{Setup, Window, display, terminal_words};
use serde_json::Value;

/// Environment that would let `ssh-keygen` sign without asking the human: a loaded ssh-agent
/// signs silently, and a forced askpass moves the question out of the window.
const SIGNING_ENV_REMOVED: [&str; 2] = ["SSH_AUTH_SOCK", "SSH_ASKPASS_REQUIRE"];
/// How long a freshly opened window may take to fail before ln-agents says it did not open.
const OPEN_WAIT: Duration = Duration::from_millis(500);

fn on_path(program: &str) -> bool {
    let Some(path) = std::env::var_os("PATH") else {
        return false;
    };
    std::env::split_paths(&path).any(|dir| {
        let candidate = dir.join(program);
        candidate.is_file() || candidate.with_extension("exe").is_file()
    })
}

/// Runs a step of `ln-agents` on the terminal the terminal UI was using, and waits for it.
#[derive(Debug)]
pub struct Here;

impl ln_tui::Foreground for Here {
    fn run(&self, cwd: &Path, args: &[String]) -> Result<i32, String> {
        let program =
            std::env::current_exe().map_err(|e| format!("cannot find ln-agents itself: {e}"))?;
        let status = Command::new(&program)
            .args(args)
            .current_dir(cwd)
            .status()
            .map_err(|e| format!("cannot run {}: {e}", program.display()))?;
        Ok(status.code().unwrap_or(1))
    }
}

/// Opens terminal windows as the machine's [`Setup`] says.
#[derive(Debug)]
pub struct Terminal(pub Setup);

impl Window for Terminal {
    /// The window is not waited for (many emulators hand over to a running instance and exit
    /// at once), only for the moment it takes a broken one to fail.
    fn open(&self, cwd: &Path, args: &[String]) -> Result<String, String> {
        let mut words =
            terminal_words(&self.0, std::env::var("TERMINAL").ok().as_deref(), &on_path)?;
        let program =
            std::env::current_exe().map_err(|e| format!("cannot find ln-agents itself: {e}"))?;
        words.push(program.display().to_string());
        words.extend(args.iter().cloned());
        let (first, rest) = words.split_first().expect("a terminal word and ln-agents");
        let mut child = Command::new(first)
            .args(rest)
            .current_dir(cwd)
            .stdin(Stdio::null())
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .spawn()
            .map_err(|e| format!("cannot open a terminal with `{first}`: {e}"))?;
        let started = Instant::now();
        while started.elapsed() < OPEN_WAIT {
            match child.try_wait() {
                Ok(Some(status)) if !status.success() => {
                    return Err(format!("`{first}` exited at once with {status}"));
                }
                Ok(Some(_)) => break,
                Ok(None) => std::thread::sleep(Duration::from_millis(20)),
                Err(e) => return Err(format!("`{first}`: {e}")),
            }
        }
        std::thread::spawn(move || {
            let _ = child.wait();
        });
        Ok(display(&words))
    }
}

/// What the flows below need from the terminal they run in; a fake in tests.
pub trait Console {
    fn interactive(&self) -> bool;
    fn say(&mut self, text: &str);
    /// One line typed after `prompt`; `None` at the end of input.
    fn ask(&mut self, prompt: &str) -> Option<String>;
    /// Run a program on this terminal with `without` removed from its environment; its exit
    /// code.
    fn run(&mut self, program: &str, args: &[String], without: &[&str]) -> io::Result<i32>;
}

/// The process's own terminal.
pub struct Tty;

impl Console for Tty {
    fn interactive(&self) -> bool {
        io::stdin().is_terminal() && io::stdout().is_terminal()
    }

    fn say(&mut self, text: &str) {
        println!("{text}");
    }

    fn ask(&mut self, prompt: &str) -> Option<String> {
        print!("{prompt}");
        let _ = io::stdout().flush();
        let mut line = String::new();
        match io::stdin().lock().read_line(&mut line) {
            Ok(0) | Err(_) => None,
            Ok(_) => Some(line.trim().to_string()),
        }
    }

    fn run(&mut self, program: &str, args: &[String], without: &[&str]) -> io::Result<i32> {
        let mut command = Command::new(program);
        command.args(args);
        for name in without {
            command.env_remove(name);
        }
        Ok(command.status()?.code().unwrap_or(1))
    }
}

/// What `ln-agents decide` is asked to sign and record.
pub struct Decision<'a> {
    pub run: &'a str,
    pub approval: &'a Path,
    pub namespace: &'a str,
    /// `None`: `~/.ssh/id_ed25519_ln_approval`, the approval key (`policy/approvers.yaml`).
    pub key: Option<&'a Path>,
    pub runs_dir: &'a Path,
    pub pipeline: &'a [String],
}

fn finish(console: &mut dyn Console, code: i32) -> i32 {
    console.ask("Press Enter to continue.");
    code
}

fn home_key() -> PathBuf {
    let home = std::env::var_os("HOME").or_else(|| std::env::var_os("USERPROFILE"));
    PathBuf::from(home.unwrap_or_default()).join(".ssh/id_ed25519_ln_approval")
}

fn pipeline_run(pipeline: &[String], tail: Vec<String>) -> Option<(String, Vec<String>)> {
    let (program, rest) = pipeline.split_first()?;
    let mut args = rest.to_vec();
    args.extend(tail);
    Some((program.clone(), args))
}

/// Sign the approval document with the human's key and record the decision. Returns the exit
/// code of the window's process.
pub fn decide(decision: &Decision, console: &mut dyn Console) -> i32 {
    if !console.interactive() {
        console.say("ERROR: deciding is done by a human at an interactive terminal");
        return 1;
    }
    let code = decide_steps(decision, console);
    finish(console, code)
}

fn decide_steps(decision: &Decision, console: &mut dyn Console) -> i32 {
    let run = decision.run;
    let document: Value = match std::fs::read_to_string(decision.approval)
        .map_err(|e| e.to_string())
        .and_then(|text| serde_json::from_str(&text).map_err(|e| e.to_string()))
    {
        Ok(document) => document,
        Err(e) => {
            console.say(&format!(
                "ERROR: cannot read {}: {e}",
                decision.approval.display()
            ));
            return 1;
        }
    };
    let field = |key: &str| document.get(key).and_then(Value::as_str).unwrap_or("-");
    if field("run_id") != run {
        console.say(&format!(
            "ERROR: that document is for run `{}`, not `{run}`; nothing was signed",
            field("run_id")
        ));
        return 1;
    }
    let evidence = document.get("evidence");
    let evidence_field = |key: &str| {
        evidence
            .and_then(|e| e.get(key))
            .map_or_else(|| "-".to_string(), |v| v.to_string().replace('"', ""))
    };
    console.say(&format!(
        "You are about to sign and record a decision.\n\n  run:        {run}\n  at:         {} ({})\n  decision:   {}\n  candidate:  {}\n  approver:   {}\n  reason:     {}\n  evidence:   provenance tip {}\n  document:   {}\n",
        field("node"),
        field("decision_type"),
        field("decision"),
        document
            .get("candidate")
            .and_then(Value::as_str)
            .unwrap_or("none"),
        field("approver"),
        field("reason"),
        evidence_field("provenance_tip"),
        decision.approval.display(),
    ));
    let report = decision
        .approval
        .with_file_name(format!("{}-evidence.md", field("node")));
    if report.is_file() {
        console.say(&format!(
            "The evidence report is {}; read it before you sign.\n",
            report.display()
        ));
    }
    let Some((program, args)) = pipeline_run(
        decision.pipeline,
        vec![
            "run".into(),
            "decide".into(),
            run.into(),
            "--approval".into(),
            decision.approval.display().to_string(),
            "--runs-dir".into(),
            decision.runs_dir.display().to_string(),
        ],
    ) else {
        console.say("ERROR: no command for the orchestrator (LN_AGENTS_PIPELINE is empty)");
        return 1;
    };
    let typed = console.ask(&format!(
        "Type the run id ({run}) to sign and record this decision: "
    ));
    if typed.as_deref() != Some(run) {
        console.say("Not confirmed; nothing was signed or recorded.");
        return 1;
    }
    let beside = |suffix: &str| {
        let mut name = decision.approval.as_os_str().to_os_string();
        name.push(suffix);
        PathBuf::from(name)
    };
    let signature = beside(".sig");
    let kept = beside(".sig.previous");
    // An earlier signature belongs to an earlier document, and ssh-keygen would ask to overwrite
    // it; set it aside, and put it back if this signing does not happen.
    let set_aside = signature.is_file();
    if set_aside && let Err(e) = std::fs::rename(&signature, &kept) {
        console.say(&format!(
            "ERROR: cannot set aside {}: {e}",
            signature.display()
        ));
        return 1;
    }
    let key = decision.key.map_or_else(home_key, Path::to_path_buf);
    console.say("ssh-keygen asks for your passphrase next.");
    let signed = console.run(
        "ssh-keygen",
        &[
            "-Y".into(),
            "sign".into(),
            "-f".into(),
            key.display().to_string(),
            "-n".into(),
            decision.namespace.into(),
            decision.approval.display().to_string(),
        ],
        &SIGNING_ENV_REMOVED,
    );
    match signed {
        Ok(0) => {
            if set_aside {
                let _ = std::fs::remove_file(&kept);
            }
        }
        failed => {
            if set_aside {
                let _ = std::fs::rename(&kept, &signature);
            }
            return match failed {
                Ok(code) => {
                    console.say(&format!(
                        "ssh-keygen failed (exit {code}); nothing was recorded."
                    ));
                    code
                }
                Err(e) => {
                    console.say(&format!(
                        "cannot run ssh-keygen: {e}; nothing was recorded."
                    ));
                    1
                }
            };
        }
    }
    match console.run(&program, &args, &[]) {
        Ok(code) => code,
        Err(e) => {
            console.say(&format!("cannot run {program}: {e}"));
            1
        }
    }
}

/// What `ln-agents resume` is asked to record.
pub struct Resumption<'a> {
    pub run: &'a str,
    pub by: &'a str,
    pub reason: &'a str,
    pub runs_dir: &'a Path,
    pub pipeline: &'a [String],
}

/// Resume a paused run. The orchestrator's own `run resume` shows why the run is paused and
/// asks the human to type the run id on this terminal; nothing is answered for it.
pub fn resume(resumption: &Resumption, console: &mut dyn Console) -> i32 {
    if !console.interactive() {
        console.say("ERROR: resuming is done by a human at an interactive terminal");
        return 1;
    }
    let Some((program, args)) = pipeline_run(
        resumption.pipeline,
        vec![
            "run".into(),
            "resume".into(),
            resumption.run.into(),
            "--by".into(),
            resumption.by.into(),
            "--reason".into(),
            resumption.reason.into(),
            "--runs-dir".into(),
            resumption.runs_dir.display().to_string(),
        ],
    ) else {
        console.say("ERROR: no command for the orchestrator (LN_AGENTS_PIPELINE is empty)");
        return 1;
    };
    let code = match console.run(&program, &args, &[]) {
        Ok(code) => code,
        Err(e) => {
            console.say(&format!("cannot run {program}: {e}"));
            1
        }
    };
    finish(console, code)
}

#[cfg(test)]
mod tests {
    use super::*;
    use ln_acp::human::DEFAULT_PIPELINE;
    use serde_json::json;

    #[derive(Default)]
    struct Fake {
        interactive: bool,
        typed: Vec<String>,
        said: Vec<String>,
        asked: Vec<String>,
        ran: Vec<(String, Vec<String>, Vec<String>)>,
        exits: Vec<i32>,
    }

    impl Console for Fake {
        fn interactive(&self) -> bool {
            self.interactive
        }
        fn say(&mut self, text: &str) {
            self.said.push(text.to_string());
        }
        fn ask(&mut self, prompt: &str) -> Option<String> {
            self.asked.push(prompt.to_string());
            if self.typed.is_empty() {
                None
            } else {
                Some(self.typed.remove(0))
            }
        }
        fn run(&mut self, program: &str, args: &[String], without: &[&str]) -> io::Result<i32> {
            self.ran.push((
                program.to_string(),
                args.to_vec(),
                without.iter().map(|w| (*w).to_string()).collect(),
            ));
            Ok(if self.exits.is_empty() {
                0
            } else {
                self.exits.remove(0)
            })
        }
    }

    fn document(dir: &Path, run: &str) -> PathBuf {
        let approvals = dir.join(".runs").join(run).join("approvals");
        std::fs::create_dir_all(&approvals).unwrap();
        let path = approvals.join("H1-approve.json");
        let doc = json!({
            "run_id": run, "node": "H1", "decision": "approve", "decision_type": "design",
            "approver": "ln-c", "candidate": "proposal_a", "reason": "looks right",
            "evidence": {"provenance_tip": "abc123"}
        });
        std::fs::write(&path, doc.to_string()).unwrap();
        path
    }

    fn scratch(name: &str) -> PathBuf {
        let dir =
            std::env::temp_dir().join(format!("ln-agents-human-{}-{name}", std::process::id()));
        let _ = std::fs::remove_dir_all(&dir);
        std::fs::create_dir_all(&dir).unwrap();
        dir
    }

    fn pipeline() -> Vec<String> {
        DEFAULT_PIPELINE.iter().map(|w| (*w).to_string()).collect()
    }

    fn decision<'a>(dir: &'a Path, doc: &'a Path, pipeline: &'a [String]) -> Decision<'a> {
        Decision {
            run: "r1",
            approval: doc,
            namespace: "ns",
            key: Some(Path::new("/keys/id")),
            runs_dir: dir,
            pipeline,
        }
    }

    #[test]
    fn deciding_shows_the_document_asks_the_run_id_then_signs_and_records() {
        let dir = scratch("decide");
        let doc = document(&dir, "r1");
        let sig = doc.with_file_name("H1-approve.json.sig");
        std::fs::write(&sig, "an earlier signature").unwrap();
        let pipeline = pipeline();
        let mut console = Fake {
            interactive: true,
            typed: vec!["r1".into(), String::new()],
            ..Fake::default()
        };
        let code = decide(&decision(&dir, &doc, &pipeline), &mut console);
        assert_eq!(code, 0);
        let shown = console.said.join("\n");
        for needle in [
            "r1",
            "H1 (design)",
            "proposal_a",
            "ln-c",
            "looks right",
            "abc123",
        ] {
            assert!(shown.contains(needle), "{needle}: {shown}");
        }
        assert_eq!(console.ran.len(), 2);
        let (program, args, without) = &console.ran[0];
        assert_eq!(program, "ssh-keygen");
        assert_eq!(
            args,
            &[
                "-Y",
                "sign",
                "-f",
                "/keys/id",
                "-n",
                "ns",
                &doc.display().to_string()
            ]
        );
        assert_eq!(
            without,
            &["SSH_AUTH_SOCK", "SSH_ASKPASS_REQUIRE"],
            "a loaded agent must not sign for the human"
        );
        assert!(
            !sig.exists(),
            "the earlier signature is replaced, not asked about"
        );
        assert!(!doc.with_file_name("H1-approve.json.sig.previous").exists());
        let (program, args, without) = &console.ran[1];
        assert_eq!(program, "uv");
        assert_eq!(
            args,
            &[
                "run".to_string(),
                "python".into(),
                "-m".into(),
                "agentic_pipeline".into(),
                "run".into(),
                "decide".into(),
                "r1".into(),
                "--approval".into(),
                doc.display().to_string(),
                "--runs-dir".into(),
                dir.display().to_string(),
            ]
        );
        assert!(without.is_empty());
        assert!(console.asked.last().unwrap().contains("Press Enter"));
    }

    #[test]
    fn nothing_is_signed_without_the_run_id() {
        let dir = scratch("decide-unconfirmed");
        let doc = document(&dir, "r1");
        let pipeline = pipeline();
        for typed in [vec!["yes".to_string()], vec![]] {
            let mut console = Fake {
                interactive: true,
                typed,
                ..Fake::default()
            };
            assert_eq!(decide(&decision(&dir, &doc, &pipeline), &mut console), 1);
            assert!(console.ran.is_empty(), "no ssh-keygen, no decide");
            assert!(console.said.join("\n").contains("nothing was signed"));
        }
    }

    #[test]
    fn nothing_is_signed_for_another_run_or_without_a_terminal() {
        let dir = scratch("decide-refused");
        let doc = document(&dir, "r1");
        let pipeline = pipeline();
        let mut console = Fake {
            interactive: false,
            typed: vec!["r1".into()],
            ..Fake::default()
        };
        assert_eq!(decide(&decision(&dir, &doc, &pipeline), &mut console), 1);
        assert!(console.ran.is_empty() && console.asked.is_empty());

        let mut other = decision(&dir, &doc, &pipeline);
        other.run = "r2";
        let mut console = Fake {
            interactive: true,
            typed: vec!["r2".into()],
            ..Fake::default()
        };
        assert_eq!(decide(&other, &mut console), 1);
        assert!(console.ran.is_empty());
        assert!(console.said.join("\n").contains("for run `r1`, not `r2`"));
    }

    #[test]
    fn a_failed_signature_records_nothing() {
        let dir = scratch("decide-failed");
        let doc = document(&dir, "r1");
        let sig = doc.with_file_name("H1-approve.json.sig");
        std::fs::write(&sig, "a signature made by hand").unwrap();
        let pipeline = pipeline();
        let mut console = Fake {
            interactive: true,
            typed: vec!["r1".into(), String::new()],
            exits: vec![255],
            ..Fake::default()
        };
        assert_eq!(decide(&decision(&dir, &doc, &pipeline), &mut console), 255);
        assert_eq!(console.ran.len(), 1, "ssh-keygen ran and decide did not");
        assert!(console.said.join("\n").contains("nothing was recorded"));
        assert_eq!(
            std::fs::read_to_string(&sig).unwrap(),
            "a signature made by hand",
            "a wrong passphrase must not cost the human the signature they had"
        );
        assert!(!doc.with_file_name("H1-approve.json.sig.previous").exists());
    }

    #[test]
    fn resuming_hands_the_terminal_to_the_orchestrators_own_confirmation() {
        let dir = scratch("resume");
        let pipeline = pipeline();
        let resumption = Resumption {
            run: "r1",
            by: "ln-c",
            reason: "quota is back",
            runs_dir: &dir,
            pipeline: &pipeline,
        };
        let mut console = Fake {
            interactive: true,
            typed: vec![String::new()],
            ..Fake::default()
        };
        assert_eq!(resume(&resumption, &mut console), 0);
        assert_eq!(console.ran.len(), 1);
        let (program, args, without) = &console.ran[0];
        assert_eq!(program, "uv");
        assert_eq!(
            &args[4..],
            [
                "run".to_string(),
                "resume".into(),
                "r1".into(),
                "--by".into(),
                "ln-c".into(),
                "--reason".into(),
                "quota is back".into(),
                "--runs-dir".into(),
                dir.display().to_string(),
            ]
        );
        assert!(without.is_empty());
        assert!(
            console.asked.iter().all(|p| p.starts_with("Press Enter")),
            "ln-agents never types the run id for the orchestrator: {:?}",
            console.asked
        );

        let mut console = Fake::default();
        assert_eq!(resume(&resumption, &mut console), 1);
        assert!(console.ran.is_empty(), "not a terminal");
    }
}
