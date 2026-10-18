//! A2A replies from `ln-pipeline`, as ACP session updates an editor shows.
//!
//! While a run advances, the states its events name become the turn's plan (the latest in
//! progress, earlier ones done; a stage's model calls are logged before the runner records
//! entering it), model calls become tool calls, and anything else is a line of text. When the
//! task stops, its status text and any evidence are the agent's message.

use agent_client_protocol_schema::v1::{
    ContentChunk, Plan, PlanEntry, PlanEntryPriority, PlanEntryStatus, SessionUpdate, ToolCall,
    ToolCallId, ToolCallStatus, ToolCallUpdate, ToolCallUpdateFields, ToolKind,
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
                        None => vec![],
                    };
                }
                self.stopped = Some(status.state.clone());
                let mut updates = vec![];
                if self.closed() && !self.entered.is_empty() {
                    updates.push(self.plan()); // every state done
                }
                updates.push(message(format!(
                    "\n{}\n",
                    stopped_text(&status.state, &text)
                )));
                updates
            }
            StreamResponse::ArtifactUpdate(update) => vec![message(artifact(&update.artifact))],
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

fn stopped_text(state: &TaskState, text: &str) -> String {
    let label = match state {
        TaskState::InputRequired => "Stopped, needs a human",
        TaskState::AuthRequired => "Paused, needs a sign-in or quota",
        TaskState::Completed => "Finished",
        TaskState::Canceled => "Cancelled",
        TaskState::Failed => "Failed",
        _ => "Stopped",
    };
    format!("**{label}.** {text}")
}

fn artifact(artifact: &Artifact) -> String {
    match (artifact.name.as_deref(), ln_a2a::data(&artifact.parts)) {
        (Some("evidence"), Some(evidence)) => evidence_text(evidence),
        (name, _) => format!("\nArtifact {}\n", name.unwrap_or("(unnamed)")),
    }
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
            "\nNext: `/evidence`, then `/approval-doc <decision> <approver> <reason>` (design \
             mode), sign it with ssh-keygen and run `run decide`.",
        );
    }
    text.push('\n');
    text
}

/// An `approval_document` skill reply: where the document is and the two human steps.
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
            None,
        ));
        assert_eq!(turn.stopped, Some(TaskState::InputRequired));
        let [SessionUpdate::AgentMessageChunk(chunk)] = updates.as_slice() else {
            panic!("{updates:?}")
        };
        let text = serde_json::to_value(&chunk.content).unwrap();
        assert!(text["text"].as_str().unwrap().contains("needs a human"));
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
