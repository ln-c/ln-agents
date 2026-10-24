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

/// A `status` skill reply as a few lines. `picks` says the front end lists what to sign for
/// `/approval-doc`, so the next step is not the long form.
pub fn status_text(status: &Value, picks: bool) -> String {
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
        text.push_str(if picks {
            "\nNext: `/evidence` to read the report, then `/approval-doc` (design mode) to pick \
             what to sign and say why; `/decide` signs it here."
        } else {
            "\nNext: `/evidence` to read the report, then `/approval-doc \
             <decision>[:<candidate>] <approver> <reason>` (design mode), sign it with ssh-keygen \
             and run `run decide`."
        });
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
    let mut text = gap_notice(catalog);
    text.push_str("\n#### Models in use\n\n");
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

/// The errors of a registry skill reply, as a list (empty when there are none).
fn reply_errors(reply: &Value) -> Vec<&str> {
    reply
        .get("errors")
        .and_then(Value::as_array)
        .map(|e| e.iter().filter_map(Value::as_str).collect())
        .unwrap_or_default()
}

/// A `registry_change` reply: the refusal, or the draft with its diff and the two ways on.
pub fn change_text(reply: &Value) -> String {
    let errors = reply_errors(reply);
    let id = reply.get("change_id").and_then(Value::as_str);
    let (Some(id), true) = (id, errors.is_empty()) else {
        let mut text = String::from("\nNot drafted, so nothing changed:\n\n");
        for error in errors {
            text.push_str(&format!("- {error}\n"));
        }
        return text;
    };
    let summary = reply.get("summary").and_then(Value::as_str).unwrap_or("");
    let mut text = String::from("\n");
    if let Some(model) = reply.get("proposed_by").and_then(Value::as_str) {
        text.push_str(&format!(
            "Proposed by `{model}`, acting as the authority. It only advises: code checked every \
             binding, and you decide.\n\n"
        ));
    }
    text.push_str(&format!("**{summary}**\n\n"));
    if let Some(says) = reply
        .get("authority_says")
        .and_then(Value::as_str)
        .filter(|s| !s.is_empty())
    {
        text.push_str(&format!("- the authority says: {says}\n"));
    }
    for note in reply
        .get("notes")
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
        .filter_map(Value::as_str)
    {
        text.push_str(&format!("- {note}\n"));
    }
    if let Some(diff) = reply.get("diff").and_then(Value::as_str) {
        text.push_str(&format!("\n```diff\n{}\n```\n", diff.trim_end()));
    }
    text.push_str(&format!(
        "\nIt passed every registry rule and nothing is written yet. To write it, type \
         `/confirm {id}`; to drop it, `/discard {id}`. A confirmed change is only an edit to \
         your working tree: you commit and push it signed.\n"
    ));
    text
}

/// The agents a `complete_mapping` reply says have no model: (kind, agent, spec).
pub fn gaps_of(reply: &Value) -> Vec<(&str, &str, &str)> {
    reply
        .get("gaps")
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
        .filter_map(|gap| {
            let get = |key: &str| gap.get(key).and_then(Value::as_str);
            Some((get("kind")?, get("agent")?, get("spec")?))
        })
        .collect()
}

/// A `guideline` reply: a verdict, then each step with where it stands, what to do and, for a
/// file to create, how it should read. A step that is not done and keeps `/start` from working
/// says so; one that does not (the coordinator, when no agent needs it) says that instead.
pub fn guideline_text(reply: &Value) -> String {
    let strings = |value: &Value, key: &str| -> Vec<String> {
        value
            .get(key)
            .and_then(Value::as_array)
            .map(|a| {
                a.iter()
                    .filter_map(Value::as_str)
                    .map(str::to_string)
                    .collect()
            })
            .unwrap_or_default()
    };
    let errors = strings(reply, "errors");
    if !errors.is_empty() {
        let mut text =
            String::from("\nThe repository could not be read, so there are no steps:\n\n");
        for error in errors {
            text.push_str(&format!("- {error}\n"));
        }
        return text;
    }
    let steps: &[Value] = reply
        .get("steps")
        .and_then(Value::as_array)
        .map(Vec::as_slice)
        .unwrap_or_default();
    if steps.is_empty() {
        return "\nThe server sent no steps; it may be older than this tool.\n".into();
    }
    fn status(step: &Value) -> &str {
        step.get("status").and_then(Value::as_str).unwrap_or("-")
    }
    let blocks = |step: &Value| step.get("blocks").and_then(Value::as_bool) == Some(true);
    let open = steps
        .iter()
        .filter(|s| blocks(s) && matches!(status(s), "todo" | "unchecked"))
        .count();
    let mut text = String::from("\n#### Getting started\n\n");
    if reply.get("ready").and_then(Value::as_bool) == Some(true) {
        text.push_str(
            "**Ready.** Nothing blocks `/start`: `/start <run> <entry_spec.json>` (design mode).\n",
        );
    } else {
        let s = if open == 1 { "" } else { "s" };
        text.push_str(&format!(
            "**Not ready.** {open} step{s} below keep{} `/start` from working. Do them in order \
             and run `/guideline` again.\n",
            if open == 1 { "s" } else { "" }
        ));
    }
    for (number, step) in steps.iter().enumerate() {
        let field = |key: &str| step.get(key).and_then(Value::as_str).unwrap_or("");
        let mark = match (status(step), blocks(step)) {
            ("done", _) => "done",
            ("auto", _) => "automatic",
            ("unchecked", _) => "not checked yet",
            ("todo", true) => "to do",
            ("todo", false) => "to do, but it does not keep `/start` from working",
            (other, _) => other,
        };
        text.push_str(&format!(
            "\n{}. **{}**: {mark}\n",
            number + 1,
            field("title")
        ));
        if !field("detail").is_empty() {
            text.push_str(&format!("   {}\n", field("detail")));
        }
        for line in strings(step, "do") {
            text.push_str(&format!("   - {line}\n"));
        }
        if let Some(snippet) = step.get("snippet").and_then(Value::as_str) {
            text.push_str("\n   ```\n");
            for line in snippet.lines() {
                text.push_str(&format!("   {line}\n"));
            }
            text.push_str("   ```\n");
        }
    }
    let notes = strings(reply, "notes");
    if !notes.is_empty() {
        text.push_str("\nNot checked here:\n\n");
        for note in notes {
            text.push_str(&format!("- {note}\n"));
        }
    }
    text
}

/// "1 agent has no model" or "3 agents have no model".
fn gap_count(count: usize) -> String {
    let (s, verb) = if count == 1 {
        ("", "has")
    } else {
        ("s", "have")
    };
    format!("{count} agent{s} {verb} no model")
}

/// One line per agent that has no model, saying why.
fn gap_lines(gaps: &[(&str, &str, &str)]) -> String {
    let mut text = String::new();
    for (kind, agent, spec) in gaps {
        if *kind == "no_entry" {
            text.push_str(&format!(
                "- `{spec}`: no entry under `agents` owns this file\n"
            ));
        } else {
            text.push_str(&format!(
                "- `agents.{agent}`: its spec `{spec}` names no model in any slot\n"
            ));
        }
    }
    text
}

/// What `/models` and `/agents` say first while agents have no model (empty when none): they
/// still answer, but no run starts and no other registry change can be drafted until `/start` gives
/// each agent a model.
fn gap_notice(reply: &Value) -> String {
    let gaps = gaps_of(reply);
    if gaps.is_empty() {
        return String::new();
    }
    format!(
        "\n**{}.** No run starts and no other registry change can be drafted until each has one. \
         `/start` proposes a model for each, and `/confirm` writes it:\n\n{}",
        gap_count(gaps.len()),
        gap_lines(&gaps)
    )
}

/// A `complete_mapping` reply that found agents with no model: no run was started. It lists
/// them, then shows the draft (or why there is none) and the way on.
pub fn gap_text(reply: &Value) -> String {
    let gaps = gaps_of(reply);
    let mut text = format!(
        "\n**{}**, so no run was started:\n\n",
        gap_count(gaps.len())
    );
    text.push_str(&gap_lines(&gaps));
    if reply.get("reused").and_then(Value::as_bool) == Some(true) {
        text.push_str(
            "\nThis is the draft from your earlier `/start`, shown again with no new model \
             call. To get a fresh proposal, `/discard` it and start again.\n",
        );
    }
    if let Some(why) = reply
        .get("authority_unavailable")
        .and_then(Value::as_str)
        .filter(|s| !s.is_empty())
    {
        text.push_str(&format!(
            "\nThe authority was not used: {why}. Code drafted the fill from the coordinator \
             named in `LN-AGENTS.md`.\n"
        ));
    }
    let mut rest = reply.clone();
    if let Some(fields) = rest.as_object_mut()
        && fields.get("proposed_by").and_then(Value::as_str) == Some("code")
    {
        fields.remove("proposed_by"); // code made this one, and the draft's summary says so
    }
    text.push_str(&change_text(&rest));
    if reply.get("change_id").and_then(Value::as_str).is_some() && reply_errors(reply).is_empty() {
        text.push_str(
            "\nAfter `/confirm`, run `/start` again. `/agents` changes any binding afterwards.\n",
        );
    }
    text
}

/// An `apply_change` reply.
pub fn applied_text(reply: &Value) -> String {
    let errors = reply_errors(reply);
    let Some(applied) = reply.get("applied").filter(|a| a.is_object()) else {
        let mut text = String::from("\nNot written, so nothing changed:\n\n");
        for error in errors {
            text.push_str(&format!("- {error}\n"));
        }
        return text;
    };
    let field = |k: &str| applied.get(k).and_then(Value::as_str).unwrap_or("-");
    format!(
        "\nWritten: {}. `{}` is changed in your working tree and not committed. Review it with \
         `git diff`, then commit and push it signed. Runs you start after that use it; a run in \
         progress keeps the policy it started with.\n",
        field("summary"),
        field("path")
    )
}

/// A `discard_change` reply.
pub fn discarded_text(reply: &Value) -> String {
    match reply.get("discarded").and_then(Value::as_str) {
        Some(summary) => format!("\nDropped: {summary}. Nothing was written.\n"),
        None => {
            let mut text = String::from("\nNothing to drop:\n\n");
            for error in reply_errors(reply) {
                text.push_str(&format!("- {error}\n"));
            }
            text
        }
    }
}

/// An `agents` reply: the slots as a table, then the drafts waiting for a human.
pub fn agents_text(reply: &Value) -> String {
    let errors = reply_errors(reply);
    if !errors.is_empty() {
        let mut text =
            String::from("\nThe repository fails its checks, so there is no mapping:\n\n");
        for error in errors {
            text.push_str(&format!("- {error}\n"));
        }
        return text;
    }
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
    let mut text = gap_notice(reply);
    text.push_str(
        "\n#### Agents and their models\n\n| slot | bound to | family | where | serves |\n|---|---|---|---|---|\n",
    );
    for row in reply
        .get("slots")
        .and_then(Value::as_array)
        .into_iter()
        .flatten()
    {
        let slot = row.get("slot").and_then(Value::as_str).unwrap_or("-");
        let models = row
            .get("models")
            .and_then(Value::as_array)
            .map(Vec::as_slice)
            .unwrap_or_default();
        let name = |m: &Value, k: &str| m.get(k).and_then(Value::as_str).unwrap_or("-").to_string();
        let (bound, family, place) = if row.get("rebindable") == Some(&Value::Bool(true)) {
            (
                models
                    .iter()
                    .map(|m| format!("`{}`", name(m, "name")))
                    .collect::<Vec<_>>()
                    .join(", "),
                models
                    .iter()
                    .map(|m| name(m, "family"))
                    .collect::<Vec<_>>()
                    .join(", "),
                models
                    .iter()
                    .map(|m| name(m, "location"))
                    .collect::<Vec<_>>()
                    .join(", "),
            )
        } else {
            let fixed = match row.get("value") {
                Some(Value::String(v)) => v.clone(),
                other => other.map(Value::to_string).unwrap_or_default(),
            };
            (format!("{fixed} (not a model)"), "-".into(), "-".into())
        };
        let serves = list(row, "operations").join(", ");
        text.push_str(&format!(
            "| `{slot}` | {} | {family} | {place} | {} |\n",
            bound.replace('|', "\\|"),
            if serves.is_empty() { "-" } else { &serves }
        ));
    }
    for (kind, agent, spec) in gaps_of(reply) {
        // an agent with no model has no slot to list, so it gets a row of its own
        let (name, why) = if kind == "no_entry" {
            (spec, "**no model** (no entry owns this spec)")
        } else {
            (agent, "**no model**")
        };
        text.push_str(&format!(
            "| `{}` | {why} | - | - | - |\n",
            name.replace('|', "\\|")
        ));
    }
    text.push_str(
        "\nBind one with `/agents <slot> <model>`; list slots take several, separated by commas. \
         `/models` shows what each model can do.\n",
    );
    let pending = reply
        .get("pending")
        .and_then(Value::as_array)
        .map(Vec::as_slice)
        .unwrap_or_default();
    if !pending.is_empty() {
        text.push_str("\n#### Waiting for you\n\n");
        for change in pending {
            let field = |k: &str| change.get(k).and_then(Value::as_str).unwrap_or("-");
            text.push_str(&format!(
                "- `{}`: {} (`/confirm {0}` or `/discard {0}`)\n",
                field("id"),
                field("summary")
            ));
        }
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

/// The same, for a person who signs with `/decide` in the same window: no commands to copy.
pub fn approval_written(reply: &Value) -> String {
    let field = |k: &str| reply.get(k).and_then(Value::as_str).unwrap_or("-");
    format!(
        "Wrote the **unsigned** {} document for `{}`: `{}`\n\nNothing is approved yet.\n",
        field("decision"),
        field("node"),
        field("document"),
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
        let text = status_text(&status, false);
        assert!(
            text.starts_with("Run `r`: **awaiting_human** at `H1`, 11 steps."),
            "{text}"
        );
        assert!(text.contains("run decide"));
        let listed = status_text(&status, true);
        assert!(
            listed.contains("`/approval-doc` (design mode) to pick"),
            "{listed}"
        );
        assert!(!listed.contains("<decision>"), "{listed}");
        let doc = approval_text(
            &json!({"node": "H1", "decision": "d", "document": "/p.json",
                                        "sign": "ssh-keygen -Y sign ...", "then": "python -m ... run decide"}),
        );
        assert!(doc.contains("**unsigned**") && doc.contains("Nothing is approved yet"));
        assert!(doc.contains("ssh-keygen -Y sign") && doc.contains("run decide"));
        let written = approval_written(
            &json!({"node": "H1", "decision": "d", "document": "/p.json",
                                        "sign": "ssh-keygen -Y sign ...", "then": "python -m ... run decide"}),
        );
        assert!(written.contains("**unsigned**") && written.contains("`/p.json`"));
        assert!(!written.contains("ssh-keygen") && !written.contains("run decide"));
        let evidence = evidence_text(&json!({"survivors": ["a"], "valid_ratios": {"x": 1.0}}));
        assert!(evidence.contains("survivors: `[\"a\"]`") && !evidence.contains("fired"));
    }

    fn gap_reply() -> Value {
        json!({
            "skill": "complete_mapping",
            "gaps": [
                {"kind": "no_entry", "agent": "extra_helper", "spec": "agents/extra-helper.md"},
                {"kind": "no_model", "agent": "learning", "spec": "agents/pipeline-learning-agent.md"},
            ],
            "change_id": "d10d897d",
            "kind": "fill_gaps",
            "summary": "Give 2 agents a model (by code: no model chose this)",
            "notes": ["agents.extra_helper is new: it serves no operation."],
            "diff": "+  extra_helper:",
            "proposed_by": "code",
            "errors": [],
        })
    }

    #[test]
    fn a_gap_reply_lists_the_agents_then_the_draft_and_the_way_on() {
        let text = gap_text(&gap_reply());
        assert!(
            text.contains("**2 agents have no model**, so no run was started"),
            "{text}"
        );
        assert!(
            text.contains("- `agents/extra-helper.md`: no entry under `agents` owns this file")
        );
        assert!(text.contains(
            "- `agents.learning`: its spec `agents/pipeline-learning-agent.md` names no model"
        ));
        assert!(text.contains("**Give 2 agents a model (by code: no model chose this)**"));
        assert!(text.contains("```diff\n+  extra_helper:\n```"));
        assert!(text.contains("`/confirm d10d897d`") && text.contains("run `/start` again"));
        assert!(
            !text.contains("acting as the authority"),
            "code made this proposal, not the authority: {text}"
        );
    }

    #[test]
    fn a_gap_reply_proposed_by_the_authority_names_it_and_what_it_said() {
        let mut reply = gap_reply();
        reply["proposed_by"] = json!("gemini_3_8_flash_high");
        reply["authority_says"] = json!("A local model suits the helper.");
        reply["summary"] = json!("Give 2 agents a model (proposed by gemini_3_8_flash_high)");
        let text = gap_text(&reply);
        assert!(
            text.contains("Proposed by `gemini_3_8_flash_high`, acting as the authority"),
            "{text}"
        );
        assert!(text.contains("- the authority says: A local model suits the helper."));
        assert!(!text.contains("The authority was not used"), "{text}");
        assert!(text.contains("`/confirm d10d897d`"));
    }

    #[test]
    fn a_gap_reply_drafted_by_code_says_why_the_authority_was_not_used() {
        let mut reply = gap_reply();
        reply["authority_unavailable"] = json!("no model is ready: gemini_3_8_flash_high");
        let text = gap_text(&reply);
        assert!(
            text.contains(
                "The authority was not used: no model is ready: gemini_3_8_flash_high. Code \
                 drafted the fill"
            ),
            "{text}"
        );
        assert!(!text.contains("acting as the authority"), "{text}");
        assert!(text.contains("`/confirm d10d897d`"));
    }

    #[test]
    fn a_refusal_still_says_why_the_authority_was_not_used() {
        let reply = json!({
            "gaps": [{"kind": "no_entry", "agent": "x", "spec": "agents/x.md"}],
            "change_id": null,
            "authority_unavailable": "the reply did not name every agent",
            "errors": ["LN-AGENTS.md names no coordinator"],
        });
        let text = gap_text(&reply);
        assert!(text.contains("The authority was not used: the reply did not name every agent"));
        assert!(
            text.contains("Not drafted") && !text.contains("`/confirm"),
            "{text}"
        );
    }

    #[test]
    fn a_draft_shown_again_says_so_and_how_to_get_a_new_one() {
        let mut reply = gap_reply();
        reply["reused"] = json!(true);
        let text = gap_text(&reply);
        assert!(
            text.contains("shown again with no new model call"),
            "{text}"
        );
        assert!(text.contains("`/discard` it and start again"), "{text}");
        assert!(!gap_text(&gap_reply()).contains("shown again"));
    }

    #[test]
    fn one_gap_reads_in_the_singular_and_a_refusal_has_no_confirm() {
        let reply = json!({
            "gaps": [{"kind": "no_entry", "agent": "x", "spec": "agents/x.md"}],
            "change_id": null,
            "errors": ["LN-AGENTS.md names no coordinator"],
        });
        let text = gap_text(&reply);
        assert!(text.contains("**1 agent has no model**"), "{text}");
        assert!(text.contains("Not drafted, so nothing changed"));
        assert!(text.contains("- LN-AGENTS.md names no coordinator"));
        assert!(
            !text.contains("/confirm") && !text.contains("/start` again"),
            "{text}"
        );
    }

    #[test]
    fn a_reply_with_no_gaps_has_none_to_list() {
        assert!(gaps_of(&json!({"gaps": [], "change_id": null, "errors": []})).is_empty());
        assert!(
            gaps_of(&json!({"errors": []})).is_empty(),
            "an absent list is no gap"
        );
        let broken = json!({"gaps": [{"kind": "no_entry"}, "x"]});
        assert!(
            gaps_of(&broken).is_empty(),
            "an entry that is missing a field is skipped"
        );
    }

    fn catalog_with_gaps() -> Value {
        json!({
            "models": [{
                "name": "qwen3_5_9b", "provider": "llama_cpp", "model_id": "qwen", "family": "qwen",
                "location": "local", "capabilities": ["code"], "bound_to": ["E1.implementation_patch"],
                "readiness": "ready", "conformance": {"status": "ok"},
            }],
            "gaps": [{"kind": "no_model", "agent": "learning", "spec": "agents/pipeline-learning-agent.md"}],
            "errors": [],
        })
    }

    #[test]
    fn models_still_shows_the_catalog_and_says_first_that_an_agent_has_no_model() {
        let text = models_text(&catalog_with_gaps());
        let notice = text.find("**1 agent has no model.**").expect(&text);
        assert!(text.contains("No run starts and no other registry change can be drafted"));
        assert!(text.contains("`/start` proposes a model for each, and `/confirm` writes it"));
        assert!(
            text.contains(
                "- `agents.learning`: its spec `agents/pipeline-learning-agent.md` names no model"
            ),
            "{text}"
        );
        let table = text.find("#### Models in use").expect(&text);
        assert!(
            notice < table && text.contains("| `qwen3_5_9b` |"),
            "the notice comes before the catalog, which is still shown: {text}"
        );
    }

    #[test]
    fn agents_says_first_which_agents_have_no_model_and_keeps_the_mapping() {
        let reply = json!({
            "slots": [{"slot": "build.primary", "value": "qwen3_5_9b", "rebindable": true,
                       "models": [{"name": "qwen3_5_9b", "family": "qwen", "location": "local"}],
                       "operations": ["E1.implementation_patch"]}],
            "pending": [],
            "gaps": [
                {"kind": "no_entry", "agent": "extra_helper", "spec": "agents/extra-helper.md"},
                {"kind": "no_model", "agent": "learning", "spec": "agents/pipeline-learning-agent.md"},
            ],
            "errors": [],
        });
        let text = agents_text(&reply);
        assert!(text.contains("**2 agents have no model.**"), "{text}");
        assert!(
            text.contains("- `agents/extra-helper.md`: no entry under `agents` owns this file")
        );
        assert!(
            text.find("have no model").unwrap()
                < text.find("#### Agents and their models").unwrap()
                && text.contains("| `build.primary` |"),
            "{text}"
        );
        // each agent with no model has a row in the table, after the slots
        let table = &text[text.find("#### Agents and their models").unwrap()..];
        assert!(
            table.contains("| `learning` | **no model** | - | - | - |\n"),
            "{text}"
        );
        assert!(
            table.contains(
                "| `agents/extra-helper.md` | **no model** (no entry owns this spec) | - | - | - |\n"
            ),
            "{text}"
        );
        assert!(
            table.find("| `build.primary` |").unwrap() < table.find("| `learning` |").unwrap(),
            "the rows follow the slots: {text}"
        );
    }

    #[test]
    fn a_pipe_in_a_gap_row_cannot_break_the_table() {
        let reply = json!({"slots": [], "pending": [], "errors": [],
                           "gaps": [{"kind": "no_entry", "agent": "x", "spec": "agents/a|b.md"}]});
        assert!(agents_text(&reply).contains("| `agents/a\\|b.md` | **no model**"));
    }

    #[test]
    fn with_no_gap_models_and_agents_say_nothing_about_one() {
        let mut catalog = catalog_with_gaps();
        catalog["gaps"] = json!([]);
        assert!(!models_text(&catalog).contains("no model"));
        catalog.as_object_mut().unwrap().remove("gaps"); // an older server sends none
        assert!(!models_text(&catalog).contains("no model"));
        let agents = json!({"slots": [], "pending": [], "gaps": [], "errors": []});
        assert!(!agents_text(&agents).contains("no model"));
    }

    #[test]
    fn a_catalog_error_is_shown_instead_of_the_notice() {
        let mut catalog = catalog_with_gaps();
        catalog["models"] = json!([]);
        catalog["errors"] = json!(["registry R1: broken"]);
        let text = models_text(&catalog);
        assert!(text.contains("registry R1: broken") && !text.contains("/confirm"));
    }
    fn guideline_reply() -> Value {
        json!({
            "ready": false,
            "steps": [
                {"id": "coordinator", "title": "Name a coordinator in LN-AGENTS.md",
                 "status": "todo", "blocks": true,
                 "detail": "There is no LN-AGENTS.md.", "do": ["Create the file:"],
                 "snippet": "---\ncoordinator: <model>\n---\n"},
                {"id": "authority", "title": "The coordinator may choose models for you (optional)",
                 "status": "todo", "blocks": false, "detail": "Not bound.", "do": [], "snippet": null},
                {"id": "repository", "title": "The repository passes its own checks",
                 "status": "done", "blocks": true, "detail": "They validate.", "do": [], "snippet": null},
                {"id": "servers", "title": "Local model servers can be started",
                 "status": "auto", "blocks": true, "detail": "Dispatch starts them.", "do": [], "snippet": null},
                {"id": "stages", "title": "Every stage has a ready model",
                 "status": "unchecked", "blocks": true, "detail": "Not checked.", "do": [], "snippet": null}
            ],
            "notes": ["Sign-in is not checked."],
            "errors": []
        })
    }

    #[test]
    fn the_guideline_says_where_it_stands_then_lists_the_steps_in_order() {
        let text = guideline_text(&guideline_reply());
        assert!(text.contains("**Not ready.** 2 steps below keep `/start` from working"));
        let order: Vec<usize> = [
            "1. **Name a coordinator in LN-AGENTS.md**: to do\n",
            "2. **The coordinator may choose models for you (optional)**: to do, but it does not keep `/start` from working\n",
            "3. **The repository passes its own checks**: done\n",
            "4. **Local model servers can be started**: automatic\n",
            "5. **Every stage has a ready model**: not checked yet\n",
        ]
        .iter()
        .map(|line| text.find(line).unwrap_or_else(|| panic!("{line}\n{text}")))
        .collect();
        assert!(order.windows(2).all(|w| w[0] < w[1]), "{text}");
        assert!(text.contains("   There is no LN-AGENTS.md.\n   - Create the file:\n"));
        assert!(
            text.contains("   ```\n   ---\n   coordinator: <model>\n   ---\n   ```\n"),
            "a file to create is shown as it should read: {text}"
        );
        assert!(text.ends_with("Not checked here:\n\n- Sign-in is not checked.\n"));
    }

    #[test]
    fn a_ready_guideline_says_to_start_and_one_open_step_is_singular() {
        let mut reply = guideline_reply();
        reply["ready"] = json!(true);
        let text = guideline_text(&reply);
        assert!(text.contains("**Ready.** Nothing blocks `/start`"));
        assert!(!text.contains("Not ready"));
        let mut one = guideline_reply();
        one["steps"] = json!([one["steps"][0].clone()]);
        assert!(guideline_text(&one).contains("1 step below keeps `/start` from working"));
    }

    #[test]
    fn a_guideline_that_could_not_read_the_repository_or_came_from_an_old_server() {
        let broken =
            guideline_text(&json!({"ready": false, "steps": [], "errors": ["OSError: gone"]}));
        assert!(broken.contains("could not be read") && broken.contains("- OSError: gone"));
        assert!(!broken.contains("Getting started"));
        assert!(guideline_text(&json!({})).contains("older than this tool"));
    }
}
