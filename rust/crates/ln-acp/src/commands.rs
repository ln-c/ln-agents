//! Slash commands: every action is deterministic code over one A2A skill, never free text a
//! model interprets. A prompt that is not a command is a question for the coordinator, answered
//! in text and never parsed into an action.

use std::fmt;

/// `status` is read-only; `design` may also start and advance runs, write approval documents
/// and open the terminal where the human signs a decision or resumes a run. Neither can approve,
/// decide or resume by itself: that is the human, in that terminal.
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
                "Also start and advance runs, write unsigned approval documents, open the \
                 terminal where you sign a decision or resume a run, and draft and confirm \
                 changes to the model registry."
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
    /// The model catalog of the repository's registry: read-only, about no run.
    Models,
    /// Draft a model for the catalog (or capabilities for a known one). Nothing is written.
    AddModel {
        provider: String,
        model_id: String,
        capabilities: Vec<String>,
    },
    /// Draft the removal of a model nothing is bound to.
    RemoveModel {
        model: String,
    },
    /// Which model each agent slot is bound to, and the drafts waiting for a human.
    Agents,
    /// The steps from naming a coordinator to a start nothing blocks: read-only, about no run.
    Guideline,
    /// Draft binding an agent slot to a model (or a list of models for a list slot).
    Bind {
        slot: String,
        models: Vec<String>,
    },
    /// Ask the authority model to propose bindings for the agent slots. It is called (it spends
    /// its allowance), and what it proposes is checked and stored as a draft, never applied.
    ProposeMapping {
        intent: String,
    },
    /// Write a draft the server stored: the human's own command, never a model's.
    Confirm {
        id: String,
    },
    Discard {
        id: String,
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
    /// Open the terminal where the human signs the unsigned document this session wrote and
    /// records the decision.
    Decide,
    /// Open the terminal where the human resumes the session's paused run.
    Resume {
        by: String,
        reason: String,
    },
    /// `/approve`, `/sign`, `/reject`: answered with what the human does instead.
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
        (
            "models",
            "",
            "The model catalog: what each model is, what it is bound to, whether it is ready",
        ),
        (
            "agents",
            "",
            "Which model each agent is bound to, and the registry changes waiting for you",
        ),
        (
            "guideline",
            "",
            "The steps from naming a coordinator to a start nothing blocks, and where each stands",
        ),
        ("help", "", "What ln-agents can and cannot do"),
    ];
    if mode == Mode::Design {
        // The same command, with what design mode adds to it: one entry per name.
        if let Some(agents) = commands.iter_mut().find(|c| c.0 == "agents") {
            *agents = (
                "agents",
                "[<slot> <model>[,<model> ...] | auto [what you want]]",
                "Which model each agent is bound to; with a slot and a model, draft binding it; \
                 `auto` asks the authority model to propose the mapping (you check the draft)",
            );
        }
        commands.extend([
            (
                "start",
                "<run> <entry_spec.json>",
                "Create a run from an entry spec and advance it (if an agent has no model, propose one first)",
            ),
            (
                "advance",
                "[run]",
                "Advance a run until it needs a human, pauses or finishes",
            ),
            (
                "approval-doc",
                "<decision>[:<candidate>] <approver> <reason>",
                "Write the unsigned approval document",
            ),
            (
                "decide",
                "",
                "Open a terminal where you sign that document and record the decision",
            ),
            (
                "resume",
                "<who> <reason>",
                "Open a terminal where you resume the run after a pause",
            ),
            (
                "add_model",
                "<provider> <model_id> [capability ...]",
                "Draft adding a model to the catalog, or claiming capabilities for a known one",
            ),
            (
                "remove_model",
                "<model>",
                "Draft removing a model nothing is bound to",
            ),
            (
                "confirm",
                "<id>",
                "Write the drafted registry change you checked; you then commit and push it signed",
            ),
            ("discard", "<id>", "Drop a drafted registry change"),
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
        "models" => match args.as_slice() {
            [] => Command::Models,
            _ => return Err(Refusal("usage: /models (it takes no arguments)".into())),
        },
        "guideline" => match args.as_slice() {
            [] => Command::Guideline,
            _ => return Err(Refusal("usage: /guideline (it takes no arguments)".into())),
        },
        "agents" => match args.as_slice() {
            [] => Command::Agents,
            ["auto", intent @ ..] => Command::ProposeMapping {
                intent: intent.join(" "),
            },
            [slot, models] => Command::Bind {
                slot: slot.to_string(),
                models: models
                    .split(',')
                    .filter(|m| !m.is_empty())
                    .map(str::to_string)
                    .collect(),
            },
            _ => {
                return Err(Refusal(
                    "usage: /agents, or /agents <slot> <model>[,<model> ...]".into(),
                ));
            }
        },
        "add_model" => match args.as_slice() {
            [provider, model_id, capabilities @ ..] => Command::AddModel {
                provider: provider.to_string(),
                model_id: model_id.to_string(),
                capabilities: capabilities.iter().map(|c| c.to_string()).collect(),
            },
            _ => {
                return Err(Refusal(
                    "usage: /add_model <provider> <model_id> [capability ...]".into(),
                ));
            }
        },
        "remove_model" => match args.as_slice() {
            [model] => Command::RemoveModel {
                model: model.to_string(),
            },
            _ => return Err(Refusal("usage: /remove_model <model>".into())),
        },
        "confirm" => match args.as_slice() {
            [id] => Command::Confirm { id: id.to_string() },
            _ => {
                return Err(Refusal(
                    "usage: /confirm <id> (the id a draft gave you)".into(),
                ));
            }
        },
        "discard" => match args.as_slice() {
            [id] => Command::Discard { id: id.to_string() },
            _ => return Err(Refusal("usage: /discard <id>".into())),
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
        "decide" => match args.as_slice() {
            [] => Command::Decide,
            _ => {
                return Err(Refusal(
                    "usage: /decide (it signs the last /approval-doc)".into(),
                ));
            }
        },
        "resume" => match args.as_slice() {
            [by, reason @ ..] if !reason.is_empty() => Command::Resume {
                by: by.to_string(),
                reason: reason.join(" "),
            },
            _ => return Err(Refusal("usage: /resume <who> <reason>".into())),
        },
        "approve" | "sign" | "reject" => {
            return Ok(Command::HumanOnly(name.to_string()));
        }
        _ => return Err(Refusal(format!("unknown command /{name}; try /help"))),
    };
    let changes = matches!(
        command,
        Command::Start { .. }
            | Command::Advance { .. }
            | Command::ApprovalDoc { .. }
            | Command::Decide
            | Command::Resume { .. }
            | Command::AddModel { .. }
            | Command::RemoveModel { .. }
            | Command::Bind { .. }
            | Command::ProposeMapping { .. }
            | Command::Confirm { .. }
            | Command::Discard { .. }
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
        "ln-agents drives the ln-pipeline orchestrator. It runs these commands as code. Anything \
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

pub const HUMAN_ONLY: &str = "\nNothing here approves for you. `/decide` opens a terminal \
     where you check what you sign, type the run id and your SSH passphrase; `/resume` opens one \
     where you confirm a resume. Whatever you type here is never your signature, and \
     `/approve`, `/sign` and `/reject` are not commands. `/confirm <id>` writes only a registry \
     change that a draft showed you, and only when you type it: no reply from a model is one.";

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
        assert_eq!(parse("/models", design), Ok(Command::Models));
        assert!(
            parse("/models qwen", design).is_err(),
            "it takes no argument"
        );
        for mode in Mode::ALL {
            assert_eq!(parse("/guideline", mode), Ok(Command::Guideline));
        }
        assert!(
            parse("/guideline now", design).is_err(),
            "it takes no argument"
        );
        assert_eq!(
            parse(
                "/add_model google_ai_pro gemini-9-new design_reasoning coordination",
                design
            ),
            Ok(Command::AddModel {
                provider: "google_ai_pro".into(),
                model_id: "gemini-9-new".into(),
                capabilities: vec!["design_reasoning".into(), "coordination".into()],
            })
        );
        assert_eq!(
            parse("/add_model google_ai_pro gemini-9-new", design),
            Ok(Command::AddModel {
                provider: "google_ai_pro".into(),
                model_id: "gemini-9-new".into(),
                capabilities: vec![],
            })
        );
        assert!(parse("/add_model google_ai_pro", design).is_err());
        assert_eq!(
            parse("/remove_model gemini_9_new", design),
            Ok(Command::RemoveModel {
                model: "gemini_9_new".into()
            })
        );
        assert!(parse("/remove_model", design).is_err());
        assert_eq!(parse("/agents", design), Ok(Command::Agents));
        assert_eq!(
            parse(
                "/agents design.proposal_b.primary gemini_4_argon_high",
                design
            ),
            Ok(Command::Bind {
                slot: "design.proposal_b.primary".into(),
                models: vec!["gemini_4_argon_high".into()],
            })
        );
        assert_eq!(
            parse("/agents human_authority.model_assistance a,b,c", design),
            Ok(Command::Bind {
                slot: "human_authority.model_assistance".into(),
                models: vec!["a".into(), "b".into(), "c".into()],
            })
        );
        assert!(parse("/agents only-a-slot x y", design).is_err());
        assert_eq!(
            parse("/agents auto", design),
            Ok(Command::ProposeMapping { intent: "".into() })
        );
        assert_eq!(
            parse("/agents auto use only local models for build", design),
            Ok(Command::ProposeMapping {
                intent: "use only local models for build".into()
            })
        );
        assert_eq!(
            parse("/confirm 1eb25dbc", design),
            Ok(Command::Confirm {
                id: "1eb25dbc".into()
            })
        );
        assert_eq!(
            parse("/discard 1eb25dbc", design),
            Ok(Command::Discard {
                id: "1eb25dbc".into()
            })
        );
        assert!(parse("/confirm", design).is_err() && parse("/confirm a b", design).is_err());
        assert_eq!(parse("/decide", design), Ok(Command::Decide));
        assert_eq!(
            parse("/resume ln-c quota is back", design),
            Ok(Command::Resume {
                by: "ln-c".into(),
                reason: "quota is back".into()
            })
        );
        assert!(parse("/decide r1", design).is_err(), "it takes no argument");
        assert!(
            parse("/resume ln-c", design).is_err(),
            "a resume needs a reason"
        );
        assert!(parse("/approval-doc d ln-c", design).is_err());
        assert!(parse("/status a b", design).is_err());
        assert!(parse("/nope", design).is_err());
    }

    #[test]
    fn status_mode_refuses_every_command_that_changes_something() {
        for text in [
            "/start r s.json",
            "/advance",
            "/approval-doc d a why",
            "/decide",
            "/resume ln-c why",
            "/add_model google_ai_pro gemini-9-new",
            "/remove_model gemini_9_new",
            "/agents design.proposal_b.primary gemini_4_argon_high",
            "/agents auto",
            "/confirm 1eb25dbc",
            "/discard 1eb25dbc",
        ] {
            let refused = parse(text, Mode::Status).unwrap_err();
            assert!(refused.0.contains("read-only"), "{text}: {refused}");
        }
        for text in [
            "/runs",
            "/status",
            "/evidence",
            "/models",
            "/agents",
            "/help",
        ] {
            assert!(parse(text, Mode::Status).is_ok(), "{text}");
        }
    }

    #[test]
    fn approving_and_signing_are_never_commands() {
        for mode in Mode::ALL {
            for name in ["approve", "sign", "reject"] {
                let parsed = parse(&format!("/{name} r1"), mode);
                assert_eq!(parsed, Ok(Command::HumanOnly(name.into())));
            }
            let offered: Vec<&str> = available(mode).iter().map(|c| c.0).collect();
            for name in ["approve", "sign", "reject"] {
                assert!(!offered.contains(&name));
            }
        }
    }

    #[test]
    fn only_design_mode_offers_the_terminals() {
        let offered = |mode| -> Vec<&'static str> { available(mode).iter().map(|c| c.0).collect() };
        for mode in Mode::ALL {
            assert!(offered(mode).contains(&"models"), "the catalog only reads");
            assert!(offered(mode).contains(&"agents"), "the mapping only reads");
            assert!(
                offered(mode).contains(&"guideline"),
                "the guideline only reads"
            );
        }
        for mode in Mode::ALL {
            let names = offered(mode);
            let unique: std::collections::HashSet<_> = names.iter().collect();
            assert_eq!(
                names.len(),
                unique.len(),
                "one entry per command name in {mode:?}"
            );
        }
        for name in ["add_model", "remove_model", "confirm", "discard"] {
            assert!(
                !offered(Mode::Status).contains(&name),
                "{name} changes a file"
            );
            assert!(offered(Mode::Design).contains(&name));
        }
        assert!(!offered(Mode::Status).contains(&"decide"));
        assert!(!offered(Mode::Status).contains(&"resume"));
        assert!(offered(Mode::Design).contains(&"decide"));
        assert!(offered(Mode::Design).contains(&"resume"));
    }
}
