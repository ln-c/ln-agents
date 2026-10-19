//! A2A replies from `ln-pipeline`, as ACP session updates an editor shows.
//!
//! While a run advances, the states its events name become the turn's plan (the latest in
//! progress, earlier ones done; a stage's model calls are logged before the runner records
//! entering it), model calls become tool calls, and anything else is a line of text. When the
//! task stops, its status text and any evidence are the agent's message.

use agent_client_protocol_schema::v1::{
    ContentBlock, ContentChunk, Plan, PlanEntry, PlanEntryPriority, PlanEntryStatus, ResourceLink,
    SessionUpdate, ToolCall, ToolCallId, ToolCallStatus, ToolCallUpdate, ToolCallUpdateFields,
    ToolKind,
};
use ln_a2a::types::{Artifact, StreamResponse, TaskState};
use serde_json::Value;

pub fn message(text: impl Into<String>) -> SessionUpdate {
    SessionUpdate::AgentMessageChunk(ContentChunk::new(text.into().into()))
}

/// What one streamed task has shown so far.
#[derive(Debug, Default)]
pub struct Turn {
    pub task: Option<(String, String)>, // (task id, run id)
    entered: Vec<String>,
    pub stopped: Option<TaskState>,
}

impl Turn {
    /// The updates for one event, in the order to send them.
    pub fn event(&mut self, event: &StreamResponse) -> Vec<SessionUpdate> {
        match event {
            StreamResponse::Task(task) => {
                let run = ln_a2a::run_id(task).unwrap_or("?").to_string();
                self.task = Some((task.id.clone(), run.clone()));
                vec![message(format!("Run `{run}`: advancing.\n"))]
            }
            StreamResponse::StatusUpdate(update) => {
                let status = &update.status;
                let text = status
                    .message
                    .as_ref()
                    .map(ln_a2a::text)
                    .unwrap_or_default();
                let data = status.message.as_ref().and_then(|m| ln_a2a::data(&m.parts));
                if status.state == TaskState::Working {
                    return match data {
                        Some(event) => self.provenance(event, &text),
                        // The server's own progress line (such as "writing the summary").
                        None if !text.is_empty() => vec![message(format!("\n_{text}…_\n"))],
                        None => vec![],
                    };
                }
                self.stopped = Some(status.state.clone());
                let mut updates = vec![];
                if self.closed() && !self.entered.is_empty() {
                    updates.push(self.plan()); // every state done
                }
                let run_status = data
                    .and_then(|d| d.get("run_status"))
                    .and_then(Value::as_str);
                updates.push(message(format!(
                    "\n{}\n",
                    stopped_text(&status.state, run_status, &text)
                )));
                updates
            }
            StreamResponse::ArtifactUpdate(update) => artifact(&update.artifact),
            StreamResponse::Message(m) => vec![message(ln_a2a::text(m))],
        }
    }

    fn provenance(&mut self, event: &Value, text: &str) -> Vec<SessionUpdate> {
        let mut updates = vec![];
        if let Some(state) = event.get("state").and_then(Value::as_str)
            && self.entered.last().map(String::as_str) != Some(state)
        {
            self.entered.push(state.to_string());
            updates.push(self.plan());
        }
        let invocation = event.get("invocation_id").and_then(Value::as_str);
        updates.extend(
            match (event.get("event").and_then(Value::as_str), invocation) {
                (Some("enter"), _) => vec![],
                (Some("model_call"), Some(id)) => vec![SessionUpdate::ToolCall(
                    ToolCall::new(ToolCallId::new(id), text)
                        .kind(ToolKind::Think)
                        .status(ToolCallStatus::InProgress)
                        .raw_input(model_fields(event)),
                )],
                (Some("model_result"), Some(id)) => {
                    let ok = event.get("reason_code").and_then(Value::as_str) == Some("ok");
                    let status = if ok {
                        ToolCallStatus::Completed
                    } else {
                        ToolCallStatus::Failed
                    };
                    vec![SessionUpdate::ToolCallUpdate(ToolCallUpdate::new(
                        ToolCallId::new(id),
                        ToolCallUpdateFields::new()
                            .status(status)
                            .title(text.to_string()),
                    ))]
                }
                _ => vec![message(format!("{text}\n"))],
            },
        );
        updates
    }

    /// Every entered state: the last in progress, the rest completed.
    fn plan(&self) -> SessionUpdate {
        let last = self.entered.len().saturating_sub(1);
        let entries = self
            .entered
            .iter()
            .enumerate()
            .map(|(i, state)| {
                let status = if i < last || self.closed() {
                    PlanEntryStatus::Completed
                } else {
                    PlanEntryStatus::InProgress
                };
                PlanEntry::new(state.clone(), PlanEntryPriority::Medium, status)
            })
            .collect();
        SessionUpdate::Plan(Plan::new(entries))
    }

    fn closed(&self) -> bool {
        matches!(self.stopped, Some(TaskState::Completed))
    }
}

fn model_fields(event: &Value) -> Value {
    let keep = [
        "state",
        "operation_id",
        "backend",
        "model",
        "model_version",
        "access_method",
    ];
    let fields = keep
        .iter()
        .filter_map(|k| event.get(*k).map(|v| ((*k).to_string(), v.clone())))
        .collect();
    Value::Object(fields)
}

/// `run_status` tells apart the stops A2A folds into INPUT_REQUIRED; only a run waiting at
/// H1, R1 or R2 is said to need a human (a server that omits it gets a plain "Stopped").
fn stopped_text(state: &TaskState, run_status: Option<&str>, text: &str) -> String {
    let label = match (state, run_status) {
        (TaskState::InputRequired, Some("awaiting_human")) => "Stopped, needs a human",
        (TaskState::InputRequired, Some("paused")) => "Paused, a human resumes it",
        (TaskState::InputRequired, Some("blocked")) => "Blocked",
        (TaskState::InputRequired, _) => "Stopped",
        (TaskState::AuthRequired, _) => "Paused, needs a sign-in or quota",
        (TaskState::Completed, _) => "Finished",
        (TaskState::Canceled, _) => "Cancelled",
        (TaskState::Failed, _) => "Failed",
        _ => "Stopped",
    };
    format!("**{label}.** {text}")
}

fn artifact(artifact: &Artifact) -> Vec<SessionUpdate> {
    match (artifact.name.as_deref(), ln_a2a::data(&artifact.parts)) {
        (Some("evidence"), Some(view)) => view_updates(view),
        (Some("summary"), Some(summary)) => summary_updates(summary),
        (name, _) => vec![message(format!(
            "\nArtifact {}\n",
            name.unwrap_or("(unnamed)")
        ))],
    }
}

/// The coordinator's summary of a stop (the `summary` artifact): its Markdown as the message, then
/// a link that opens the file. The file's leading HTML comment is for tools, not for the chat.
pub fn summary_updates(summary: &Value) -> Vec<SessionUpdate> {
    let Some(markdown) = summary.get("markdown").and_then(Value::as_str) else {
        return vec![message("\nSummary\n")];
    };
    let body: Vec<&str> = markdown
        .lines()
        .skip_while(|line| line.trim_start().starts_with("<!--"))
        .collect();
    let mut updates = vec![message(format!("\n{}\n", body.join("\n")))];
    if let Some(path) = summary.get("path").and_then(Value::as_str) {
        updates.extend(link_updates(path, "This summary as a file: "));
    }
    updates
}

/// A resource link that opens `path` in the editor, after a one-line lead-in.
fn link_updates(path: &str, lead: &str) -> Vec<SessionUpdate> {
    let name = std::path::Path::new(path)
        .file_name()
        .map(|n| n.to_string_lossy().into_owned())
        .unwrap_or_else(|| path.to_string());
    let link = ResourceLink::new(name, format!("file://{path}"))
        .mime_type("text/markdown".to_string())
        .title("Open the file".to_string());
    vec![
        message(lead),
        SessionUpdate::AgentMessageChunk(ContentChunk::new(ContentBlock::ResourceLink(link))),
        message("\n"),
    ]
}

/// An `ask` skill reply: the coordinator's text, and which of the run's files it was given.
pub fn answer_text(reply: &Value) -> String {
    let answer = reply
        .get("answer")
        .and_then(Value::as_str)
        .unwrap_or("(no answer)");
    let sources: Vec<&str> = reply
        .get("sources")
        .and_then(Value::as_array)
        .map(|s| s.iter().filter_map(Value::as_str).collect())
        .unwrap_or_default();
    let from = if sources.is_empty() {
        String::new()
    } else {
        format!("\n_From: {}_\n", sources.join(", "))
    };
    format!("\n{answer}\n{from}")
}

/// The sections of the report shown in the chat; the rest is one click away in the file.
const SUMMARY_SECTIONS: [&str; 2] = ["## Your choices", "## Candidates at a glance"];

/// An approval view (the `evidence` skill reply, also attached when a run stops for a human):
/// the choices and the candidates at a glance from its Markdown report, then a link that opens
/// the whole report. A server without reports sends bare evidence, shown as before.
pub fn view_updates(view: &Value) -> Vec<SessionUpdate> {
    let Some(markdown) = view.get("markdown").and_then(Value::as_str) else {
        return vec![message(evidence_text(view))];
    };
    let mut text = String::from("\n");
    for heading in SUMMARY_SECTIONS {
        if let Some(section) = section(markdown, heading) {
            text.push_str(&section.replacen("## ", "#### ", 1));
            text.push('\n');
        }
    }
    let mut updates = vec![message(text)];
    if let Some(path) = view.get("path").and_then(Value::as_str) {
        updates.extend(link_updates(
            path,
            "The full report, with every finding and what you sign: ",
        ));
    }
    updates
}

/// From `heading` up to the next section of the same level.
fn section<'a>(markdown: &'a str, heading: &str) -> Option<&'a str> {
    let start = markdown
        .match_indices(heading)
        .map(|(i, _)| i)
        .find(|&i| i == 0 || markdown[..i].ends_with('\n'))?;
    let body = &markdown[start + heading.len()..];
    let end = body
        .find("\n## ")
        .map_or(markdown.len(), |i| start + heading.len() + i + 1);
    Some(&markdown[start..end])
}

/// The parts of H1/R1/R2 evidence a reviewer reads first; `/evidence` shows all of it.
pub fn evidence_text(evidence: &Value) -> String {
    let field = |k: &str| {
        evidence
            .get(k)
            .map(Value::to_string)
            .unwrap_or_else(|| "-".into())
    };
    let mut text = String::from("\n**Evidence** (what a human signs; `/evidence` for all of it)\n");
    for key in [
        "survivors",
        "fired",
        "undetermined",
        "valid_ratios",
        "waived_blocking",
    ] {
        if evidence.get(key).is_some() {
            text.push_str(&format!("- {key}: `{}`\n", field(key)));
        }
    }
    for key in ["commit_sha", "context_manifest"] {
        if evidence.get(key).is_some() {
            text.push_str(&format!("- {key}: `{}`\n", field(key).trim_matches('"')));
        }
    }
    text
}

/// A `status` skill reply as a few lines.
pub fn status_text(status: &Value) -> String {
    let field = |k: &str| match status.get(k) {
        Some(Value::String(s)) => s.clone(),
        Some(Value::Null) | None => "-".into(),
        Some(Value::Number(n)) => n.as_f64().map(|f| format!("{f:.0}")).unwrap_or_default(),
        Some(other) => other.to_string(),
    };
    let mut text = format!(
        "Run `{}`: **{}** at `{}`, {} steps",
        field("run_id"),
        field("status"),
        field("current"),
        field("steps")
    );
    if status.get("runner_active") == Some(&Value::Bool(true)) {
        text.push_str(", being advanced now");
    }
    text.push('.');
    if let Some(Value::String(reason)) = status.get("paused_reason") {
        text.push_str(&format!("\nPaused: {reason}"));
    }
    if field("status") == "awaiting_human" {
        text.push_str(
            "\nNext: `/evidence` to read the report, then `/approval-doc \
             <decision>[:<candidate>] <approver> <reason>` (design mode), sign it with ssh-keygen \
             and run `run decide`.",
        );
    }
    text.push('\n');
    text
}

/// A `models` skill reply: the models something is bound to as a table, then the rest by name.
pub fn models_text(catalog: &Value) -> String {
    let rows: &[Value] = catalog
        .get("models")
        .and_then(Value::as_array)
        .map(Vec::as_slice)
        .unwrap_or_default();
    let errors: Vec<&str> = catalog
        .get("errors")
        .and_then(Value::as_array)
        .map(|e| e.iter().filter_map(Value::as_str).collect())
        .unwrap_or_default();
    if !errors.is_empty() {
        let mut text = String::from(
            "\nThe repository fails its checks, so there is no catalog to show. Fix these (`python -m agentic_pipeline validate` lists them):\n\n",
        );
        for error in errors {
            text.push_str(&format!("- {error}\n"));
        }
        return text;
    }
    let field = |row: &Value, key: &str| {
        row.get(key)
            .and_then(Value::as_str)
            .unwrap_or("-")
            .to_string()
    };
    let list = |row: &Value, key: &str| -> Vec<String> {
        row.get(key)
            .and_then(Value::as_array)
            .map(|a| {
                a.iter()
                    .filter_map(Value::as_str)
                    .map(str::to_string)
                    .collect()
            })
            .unwrap_or_default()
    };
    let cell = |text: &str| text.replace('|', "\\|");
    let short = |text: &str| match text.char_indices().nth(70) {
        Some((end, _)) => format!("{}…", &text[..end]),
        None => text.to_string(),
    };
    let mut bound = String::new();
    let (mut bindable, mut unclaimed) = (vec![], vec![]);
    for row in rows {
        let name = field(row, "name");
        let operations = list(row, "bound_to");
        if operations.is_empty() {
            if list(row, "capabilities").is_empty() {
                unclaimed.push(format!("`{name}`"));
            } else {
                bindable.push(format!(
                    "`{name}` ({})",
                    list(row, "capabilities").join(", ")
                ));
            }
            continue;
        }
        let readiness = match row.get("readiness").and_then(Value::as_str) {
            Some("ready") => "ready".to_string(),
            Some(why) => format!("not ready: {}", short(why)),
            None => "-".to_string(),
        };
        let conformance = row.get("conformance").map_or("-".to_string(), |c| {
            let status = c.get("status").and_then(Value::as_str).unwrap_or("-");
            match c.get("detail").and_then(Value::as_str) {
                Some(detail) if status != "ok" => format!("{status}: {}", short(detail)),
                _ => status.to_string(),
            }
        });
        bound.push_str(&format!(
            "| `{name}` | {}:{} | {} | {} | {} | {} | {} | {} |\n",
            field(row, "provider"),
            field(row, "model_id"),
            field(row, "family"),
            field(row, "location"),
            cell(&list(row, "capabilities").join(", ")),
            cell(&operations.join(", ")),
            cell(&readiness),
            cell(&conformance),
        ));
    }
    let mut text = String::from("\n#### Models in use\n\n");
    if bound.is_empty() {
        text.push_str("None is bound to an operation.\n");
    } else {
        text.push_str(
            "| model | provider:id | family | where | capabilities | bound to | readiness | conformance |\n|---|---|---|---|---|---|---|---|\n",
        );
        text.push_str(&bound);
    }
    if !bindable.is_empty() {
        text.push_str(&format!(
            "\n#### Known, not bound (they declare capabilities, so a human can bind them)\n\n{}\n",
            bindable.join(", ")
        ));
    }
    if !unclaimed.is_empty() {
        text.push_str(&format!(
            "\n#### Known, unclaimed (no capability is claimed, so none can be bound)\n\n{}\n",
            unclaimed.join(", ")
        ));
    }
    text
}

/// An `approval_document` skill reply: where the document is and the two human steps.
/// `/decide` or `/resume` opened a terminal window for the human.
pub fn window_opened(run: &str, what: &str, command: &str) -> String {
    format!(
        "A terminal window is open for run `{run}`. In it you {what}: you type the run id there, \
         and for a decision your SSH passphrase; ln-agents never sees either. When the window says \
         it is done, `/status` shows where the run is and `/advance` continues it.\n\n\
         Opened with `{command}`\n"
    )
}

/// No terminal window could be opened; the human does it by hand.
pub fn window_failed(error: &str, manual: &str) -> String {
    format!(
        "No terminal window was opened: {error}.\n\nDo it by hand in a terminal instead:\n\n```sh\n{manual}\n```\n"
    )
}

pub fn approval_text(reply: &Value) -> String {
    let field = |k: &str| reply.get(k).and_then(Value::as_str).unwrap_or("-");
    format!(
        "Wrote the **unsigned** {} document for `{}`: `{}`\n\nNothing is approved yet. To \
         approve, a human runs:\n\n```sh\n{}\n{}\n```\n",
        field("decision"),
        field("node"),
        field("document"),
        field("sign"),
        field("then"),
    )
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn status_update(state: &str, text: &str, data: Option<Value>) -> StreamResponse {
        let mut parts = vec![json!({"text": text})];
        parts.extend(data.map(|d| json!({"data": d})));
        serde_json::from_value(json!({"statusUpdate": {
            "taskId": "t", "contextId": "c",
            "status": {"state": state, "message": {"messageId": "m", "role": "ROLE_AGENT", "parts": parts}}
        }}))
        .unwrap()
    }

    #[test]
    fn a_catalog_with_errors_shows_the_errors_and_no_table() {
        let text = models_text(&json!({"models": [], "errors": ["registry R1: broken"]}));
        assert!(text.contains("registry R1: broken") && !text.contains("| model |"));
    }

    #[test]
    fn a_pipe_in_a_cell_cannot_break_the_table() {
        let text = models_text(&json!({"models": [{
            "name": "m", "provider": "p", "model_id": "i", "family": "f", "location": "local",
            "capabilities": ["a|b"], "bound_to": ["A1.x"], "readiness": "ready",
            "conformance": {"status": "ok", "detail": "recorded"}
        }], "errors": []}));
        assert!(text.contains("a\\|b") && text.contains("| ready | ok |"));
    }

    #[test]
    fn nothing_bound_says_so_and_a_missing_catalog_is_empty_not_a_panic() {
        assert!(models_text(&json!({"models": [], "errors": []})).contains("None is bound"));
        assert!(models_text(&json!({})).contains("None is bound"));
    }

    #[test]
    fn states_become_the_plan_and_model_calls_tool_calls() {
        let mut turn = Turn::default();
        let working =
            |text: &str, data: Value| status_update("TASK_STATE_WORKING", text, Some(data));
        turn.event(&working(
            "entered G",
            json!({"event": "enter", "state": "G"}),
        ));
        // A stage's calls are logged before the runner records entering it.
        let call = json!({"event": "model_call", "state": "A1", "operation_id": "proposal_a",
                          "invocation_id": "i1", "model": "m", "prompt_hash": "sha256:x"});
        let updates = turn.event(&working("A1.proposal_a: calling m", call));
        let [SessionUpdate::Plan(plan), SessionUpdate::ToolCall(tool)] = updates.as_slice() else {
            panic!("{updates:?}")
        };
        let statuses: Vec<_> = plan
            .entries
            .iter()
            .map(|e| (e.content.as_str(), e.status.clone()))
            .collect();
        assert_eq!(
            statuses,
            [
                ("G", PlanEntryStatus::Completed),
                ("A1", PlanEntryStatus::InProgress)
            ]
        );
        assert_eq!(
            (tool.tool_call_id.0.as_ref(), tool.status),
            ("i1", ToolCallStatus::InProgress)
        );
        assert!(
            tool.raw_input
                .as_ref()
                .unwrap()
                .get("prompt_hash")
                .is_none()
        );

        let result = json!({"event": "model_result", "state": "A1", "invocation_id": "i1",
                            "reason_code": "quota_exhausted"});
        let updates = turn.event(&working("A1.proposal_a: quota_exhausted", result));
        let [SessionUpdate::ToolCallUpdate(update)] = updates.as_slice() else {
            panic!("{updates:?}")
        };
        assert_eq!(update.fields.status, Some(ToolCallStatus::Failed));
        let updates = turn.event(&working(
            "entered A1",
            json!({"event": "enter", "state": "A1"}),
        ));
        assert!(updates.is_empty(), "{updates:?}");
    }

    #[test]
    fn a_stop_names_why_and_keeps_the_waiting_state_in_progress() {
        let mut turn = Turn::default();
        turn.event(&status_update(
            "TASK_STATE_WORKING",
            "entered H1",
            Some(json!({"event": "enter", "state": "H1"})),
        ));
        let updates = turn.event(&status_update(
            "TASK_STATE_INPUT_REQUIRED",
            "waiting for a signed approval at H1",
            Some(json!({"run_status": "awaiting_human", "node": "H1"})),
        ));
        assert_eq!(turn.stopped, Some(TaskState::InputRequired));
        let [SessionUpdate::AgentMessageChunk(chunk)] = updates.as_slice() else {
            panic!("{updates:?}")
        };
        let text = serde_json::to_value(&chunk.content).unwrap();
        assert!(text["text"].as_str().unwrap().contains("needs a human"));
    }

    #[test]
    fn only_a_wait_at_a_human_state_says_it_needs_a_human() {
        let stop = |run_status: Option<&str>| {
            let data = run_status.map(|s| json!({"run_status": s, "node": "A2"}));
            let mut turn = Turn::default();
            let updates = turn.event(&status_update(
                "TASK_STATE_INPUT_REQUIRED",
                "blocked at A2: handler_error",
                data,
            ));
            let [SessionUpdate::AgentMessageChunk(chunk)] = updates.as_slice() else {
                panic!("{updates:?}")
            };
            serde_json::to_value(&chunk.content).unwrap()["text"]
                .as_str()
                .unwrap()
                .to_string()
        };
        assert!(stop(Some("blocked")).contains("**Blocked.** blocked at A2"));
        assert!(stop(Some("paused")).contains("**Paused, a human resumes it.**"));
        for quiet in [Some("out_of_steps"), Some("stopped"), None] {
            let text = stop(quiet);
            assert!(
                text.contains("**Stopped.**") && !text.contains("human"),
                "{text}"
            );
        }
    }

    #[test]
    fn a_view_shows_the_choices_and_glance_then_links_the_whole_report() {
        let markdown = "# Design decision for run `r` (H1)\n\nIntro.\n\n## Your choices\n\n\
                        - **Approve the synthesis** (`approve`)\n\n## Candidates at a glance\n\n\
                        | Candidate | Rank |\n|---|---|\n| the synthesis | 1 |\n\n\
                        ## Design A (`proposal_a_sealed`)\n\nLong text.\n";
        let view = json!({"markdown": markdown, "path": "/w/.runs/r/approvals/H1-evidence.md"});
        let updates = view_updates(&view);
        let [
            SessionUpdate::AgentMessageChunk(summary),
            SessionUpdate::AgentMessageChunk(_),
            SessionUpdate::AgentMessageChunk(link),
            SessionUpdate::AgentMessageChunk(_),
        ] = updates.as_slice()
        else {
            panic!("{updates:?}")
        };
        let summary = serde_json::to_value(&summary.content).unwrap()["text"]
            .as_str()
            .unwrap()
            .to_string();
        assert!(summary.contains("#### Your choices") && summary.contains("Approve the synthesis"));
        assert!(summary.contains("| the synthesis | 1 |"), "{summary}");
        assert!(
            !summary.contains("Long text") && !summary.contains("Intro"),
            "{summary}"
        );
        let link = serde_json::to_value(&link.content).unwrap();
        assert_eq!(link["type"], "resource_link");
        assert_eq!(link["uri"], "file:///w/.runs/r/approvals/H1-evidence.md");
        assert_eq!(link["name"], "H1-evidence.md");
        // An older server sends bare evidence: shown as before, nothing invented.
        let old = view_updates(&json!({"survivors": ["a"]}));
        let [SessionUpdate::AgentMessageChunk(old)] = old.as_slice() else {
            panic!("{old:?}")
        };
        let old = serde_json::to_value(&old.content).unwrap();
        assert!(old["text"].as_str().unwrap().contains("survivors"));
    }

    #[test]
    fn replies_render_without_inventing_fields() {
        let status = json!({"run_id": "r", "status": "awaiting_human", "current": "H1", "steps": 11.0,
                            "runner_active": false, "paused_reason": null});
        let text = status_text(&status);
        assert!(
            text.starts_with("Run `r`: **awaiting_human** at `H1`, 11 steps."),
            "{text}"
        );
        assert!(text.contains("run decide"));
        let doc = approval_text(
            &json!({"node": "H1", "decision": "d", "document": "/p.json",
                                        "sign": "ssh-keygen -Y sign ...", "then": "python -m ... run decide"}),
        );
        assert!(doc.contains("**unsigned**") && doc.contains("Nothing is approved yet"));
        let evidence = evidence_text(&json!({"survivors": ["a"], "valid_ratios": {"x": 1.0}}));
        assert!(evidence.contains("survivors: `[\"a\"]`") && !evidence.contains("fired"));
    }
}
