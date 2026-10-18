//! Slash commands: every action is deterministic code over one A2A skill, never free text a
//! model interprets. A prompt that is not a command gets the help text.

use std::fmt;

/// `status` is read-only; `design` may also start and advance runs and write approval
/// documents. Neither can approve, decide or resume: that is a human with ssh-keygen.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Mode {
    Status,
    Design,
}

impl Mode {
    pub const ALL: [Mode; 2] = [Mode::Status, Mode::Design];

    pub fn id(self) -> &'static str {
        match self {
            Mode::Status => "status",
            Mode::Design => "design",
        }
    }

    pub fn from_id(id: &str) -> Option<Mode> {
        Mode::ALL.into_iter().find(|m| m.id() == id)
    }

    pub fn name(self) -> &'static str {
        match self {
            Mode::Status => "Status (read-only)",
            Mode::Design => "Design",
        }
    }

    pub fn description(self) -> &'static str {
        match self {
            Mode::Status => "List runs and read their status and evidence; changes nothing.",
            Mode::Design => {
                "Also start and advance runs and write unsigned approval documents. \
                 Approving is still signing with ssh-keygen and `run decide`."
            }
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Command {
    Help,
    Runs,
    Status {
        run: Option<String>,
    },
    Evidence {
        run: Option<String>,
    },
    /// Anything that is not a slash command: a question for the coordinator model, answered in
    /// text. It is never parsed into an action.
    Ask {
        question: String,
    },
    Start {
        run: String,
        entry_spec: String,
    },
    Advance {
        run: Option<String>,
    },
    ApprovalDoc {
        decision: String,
        candidate: Option<String>,
        approver: String,
        reason: String,
    },
    /// `/approve`, `/decide`, `/resume`, `/sign`: answered with what the human does instead.
    HumanOnly(String),
}

/// Name, input hint and description of each command a mode offers, for
/// `available_commands_update`.
pub fn available(mode: Mode) -> Vec<(&'static str, &'static str, &'static str)> {
    let mut commands = vec![
        ("runs", "", "List the runs the orchestrator knows over A2A"),
        (
            "status",
            "[run]",
            "Where a run is, its status, budget and pause reason",
        ),
        (
            "evidence",
            "[run]",
            "The evidence a human signs at H1, R1 or R2",
        ),
        ("help", "", "What ln-hub can and cannot do"),
    ];
    if mode == Mode::Design {
        commands.extend([
            (
                "start",
                "<run> <entry_spec.json>",
                "Create a run from an entry spec and advance it",
            ),
            (
                "advance",
                "[run]",
                "Advance a run until it needs a human, pauses or finishes",
            ),
            (
                "approval-doc",
                "<decision>[:<candidate>] <approver> <reason>",
                "Write the unsigned approval document and show how to sign it",
            ),
        ]);
    }
    commands
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Refusal(pub String);

impl fmt::Display for Refusal {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        f.write_str(&self.0)
    }
}

/// The command in a prompt's text, or a refusal explaining why there is none.
pub fn parse(text: &str, mode: Mode) -> Result<Command, Refusal> {
    let text = text.trim();
    let Some(rest) = text.strip_prefix('/') else {
        return Ok(match text {
            "" | "help" | "?" => Command::Help,
            question => Command::Ask {
                question: question.to_string(),
            },
        });
    };
    let mut words = rest.split_whitespace();
    let name = words.next().unwrap_or_default();
    let args: Vec<&str> = words.collect();
    let optional_run = |args: &[&str]| match args {
        [] => Ok(None),
        [run] => Ok(Some((*run).to_string())),
        _ => Err(Refusal(format!("/{name} takes at most one run id"))),
    };
    let command = match name {
        "help" => Command::Help,
        "runs" => Command::Runs,
        "status" => Command::Status {
            run: optional_run(&args)?,
        },
        "evidence" => Command::Evidence {
            run: optional_run(&args)?,
        },
        "advance" => Command::Advance {
            run: optional_run(&args)?,
        },
        "start" => match args.as_slice() {
            [run, spec] => Command::Start {
                run: run.to_string(),
                entry_spec: spec.to_string(),
            },
            _ => return Err(Refusal("usage: /start <run> <entry_spec.json>".into())),
        },
        "approval-doc" => match args.as_slice() {
            [choice, approver, reason @ ..] if !reason.is_empty() => {
                let (decision, candidate) = match choice.split_once(':') {
                    Some((decision, candidate)) => (decision, Some(candidate.to_string())),
                    None => (*choice, None),
                };
                Command::ApprovalDoc {
                    decision: decision.to_string(),
                    candidate,
                    approver: approver.to_string(),
                    reason: reason.join(" "),
                }
            }
            _ => {
                return Err(Refusal(
                    "usage: /approval-doc <decision>[:<candidate>] <approver> <reason>".into(),
                ));
            }
        },
        "approve" | "decide" | "resume" | "sign" | "reject" => {
            return Ok(Command::HumanOnly(name.to_string()));
        }
        _ => return Err(Refusal(format!("unknown command /{name}; try /help"))),
    };
    let changes = matches!(
        command,
        Command::Start { .. } | Command::Advance { .. } | Command::ApprovalDoc { .. }
    );
    if changes && mode == Mode::Status {
        return Err(Refusal(format!(
            "/{name} is not available in the read-only status mode; switch to design mode"
        )));
    }
    Ok(command)
}

pub fn help(mode: Mode) -> String {
    let mut text = String::from(
        "ln-hub drives the ln-pipeline orchestrator. It runs these commands as code. Anything \
         else you type is a question for the coordinator model: it answers in text from the \
         run's Markdown views, and cannot start, advance, sign or resume anything.\n\n",
    );
    for (name, hint, description) in available(mode) {
        let usage = if hint.is_empty() {
            format!("/{name}")
        } else {
            format!("/{name} {hint}")
        };
        text.push_str(&format!("- `{usage}`: {description}\n"));
    }
    text.push_str(&format!(
        "\nMode: {}. {}\n",
        mode.name(),
        mode.description()
    ));
    text.push_str(HUMAN_ONLY);
    text
}

pub const HUMAN_ONLY: &str = "\nApproving, deciding and resuming are not commands here: a human \
     signs the approval document with `ssh-keygen -Y sign` and runs \
     `python -m agentic_pipeline run decide` (or `run resume`) in a terminal.";

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn commands_parse_with_their_arguments() {
        let design = Mode::Design;
        assert_eq!(parse("/runs", design), Ok(Command::Runs));
        assert_eq!(
            parse("  /status r1 ", design),
            Ok(Command::Status {
                run: Some("r1".into())
            })
        );
        assert_eq!(
            parse("/evidence", design),
            Ok(Command::Evidence { run: None })
        );
        assert_eq!(
            parse("/start r1 spec.json", design),
            Ok(Command::Start {
                run: "r1".into(),
                entry_spec: "spec.json".into()
            })
        );
        assert_eq!(
            parse(
                "/approval-doc approve_design_and_no_security_veto ln-c looks right",
                design
            ),
            Ok(Command::ApprovalDoc {
                decision: "approve_design_and_no_security_veto".into(),
                candidate: None,
                approver: "ln-c".into(),
                reason: "looks right".into(),
            })
        );
        assert_eq!(
            parse(
                "/approval-doc approve_design_and_no_security_veto:synthesis_sealed ln-c ok",
                design
            ),
            Ok(Command::ApprovalDoc {
                decision: "approve_design_and_no_security_veto".into(),
                candidate: Some("synthesis_sealed".into()),
                approver: "ln-c".into(),
                reason: "ok".into(),
            })
        );
        assert_eq!(
            parse("why is the synthesis first?", design),
            Ok(Command::Ask {
                question: "why is the synthesis first?".into()
            })
        );
        assert_eq!(parse("  help ", design), Ok(Command::Help));
        assert_eq!(parse("", design), Ok(Command::Help));
        assert!(parse("/start r1", design).is_err());
        assert!(parse("/approval-doc d ln-c", design).is_err());
        assert!(parse("/status a b", design).is_err());
        assert!(parse("/nope", design).is_err());
    }

    #[test]
    fn status_mode_refuses_every_command_that_changes_something() {
        for text in ["/start r s.json", "/advance", "/approval-doc d a why"] {
            let refused = parse(text, Mode::Status).unwrap_err();
            assert!(refused.0.contains("read-only"), "{text}: {refused}");
        }
        for text in ["/runs", "/status", "/evidence", "/help"] {
            assert!(parse(text, Mode::Status).is_ok(), "{text}");
        }
    }

    #[test]
    fn approving_deciding_and_resuming_are_human_only_in_every_mode() {
        for mode in Mode::ALL {
            for name in ["approve", "decide", "resume", "sign", "reject"] {
                let parsed = parse(&format!("/{name} r1"), mode);
                assert_eq!(parsed, Ok(Command::HumanOnly(name.into())));
            }
            let offered: Vec<&str> = available(mode).iter().map(|c| c.0).collect();
            for name in ["approve", "decide", "resume", "sign", "reject"] {
                assert!(!offered.contains(&name));
            }
        }
    }
}
