//! What the terminal UI knows: the transcript, the stages of the run being driven, the input
//! line, and what a key or a message from a turn changes. Nothing here touches a terminal, so all
//! of it is tested without one.

use std::path::PathBuf;
use std::sync::mpsc::Sender;
use std::time::Instant;

use agent_client_protocol_schema::v1::{
    ContentBlock, PlanEntryStatus, SessionUpdate, StopReason, ToolCallStatus,
};
use crossterm::event::{KeyCode, KeyEvent, KeyEventKind, KeyModifiers};
use ln_acp::commands::{self, Mode};
use ratatui::style::{Modifier, Style};
use ratatui::text::{Line, Span};
use ratatui::widgets::Block as Frame;
use tui_textarea::{CursorMove, TextArea, WrapMode};

use crate::markdown::{self, Theme};

/// The tallest the input grows, borders included.
pub const INPUT_ROWS: u16 = 5;

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum ToolState {
    Running,
    Done,
    Failed,
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum Block {
    /// What the person typed.
    User(String),
    /// The agent's Markdown; chunks of one message are joined.
    Agent(String),
    /// A model call or other step, one line.
    Tool {
        id: String,
        title: String,
        state: ToolState,
    },
    /// Something ln-agents itself says: the welcome, a refusal, how a cancel went.
    Notice(String),
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum StageState {
    Done,
    Working,
    Pending,
}

/// One state of the run's graph, as the turn's plan names it.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct Stage {
    pub name: String,
    pub state: StageState,
}

/// What a worker thread tells the UI.
pub enum Msg {
    Update(Box<SessionUpdate>),
    /// The session now follows this run.
    Bound(String),
    /// A turn wants to write an unsigned document: answer `true` once to allow it.
    Ask {
        title: String,
        reply: Sender<bool>,
    },
    /// A turn hands the person a step that runs in this terminal; the reply says how it went.
    Foreground {
        cwd: PathBuf,
        args: Vec<String>,
        /// The exit status of the step, or why it could not start.
        reply: Sender<Result<i32, String>>,
    },
    /// The turn is over.
    Done {
        stop: StopReason,
        failed: bool,
    },
}

/// What the loop does for the App.
#[derive(Debug, PartialEq, Eq)]
pub enum Action {
    /// Run this text as one turn.
    Run {
        text: String,
        mode: Mode,
        run: Option<String>,
    },
    /// Redraw everything.
    Redraw,
}

pub struct Running {
    pub started: Instant,
    pub cancelling: bool,
    cancel: Box<dyn Fn() + Send>,
}

pub struct Question {
    pub title: String,
    reply: Sender<bool>,
}

struct Item {
    block: Block,
    rev: u32,
    cache: Option<(u16, u32, Vec<Line<'static>>)>,
}

pub struct App {
    items: Vec<Item>,
    /// The first item not yet written into the terminal's scrollback (inline screen).
    emitted: usize,
    pub stages: Vec<Stage>,
    pub input: TextArea<'static>,
    history: Vec<String>,
    history_at: Option<usize>,
    draft: String,
    pub mode: Mode,
    pub run: Option<String>,
    pub running: Option<Running>,
    pub question: Option<Question>,
    /// Lines scrolled up from the bottom of the transcript; 0 follows the newest.
    pub up: usize,
    pub total: usize,
    pub quit: bool,
    pub theme: Theme,
    streaming: bool,
}

/// Commands the UI itself answers, with their hints.
const LOCAL: [(&str, &str, &str); 4] = [
    ("mode", "[status|design]", "Show or set the mode"),
    ("clear", "", "Clear the transcript"),
    ("help", "", "The commands and the keys"),
    ("quit", "", "Leave ln-agents"),
];

/// The commands a mode offers in this UI. `/decide` and `/resume` run in this terminal, which is
/// not what the editor's list says.
pub fn commands_for(mode: Mode) -> Vec<(String, String, String)> {
    let mut list: Vec<(String, String, String)> = commands::available(mode)
        .into_iter()
        .filter(|(name, _, _)| *name != "help")
        .map(|(name, hint, description)| {
            let description = match name {
                "decide" => "Sign that document here, with your key, and record the decision",
                "resume" => "Confirm here that the paused run may go on",
                _ => description,
            };
            (name.to_string(), hint.to_string(), description.to_string())
        })
        .collect();
    list.extend(
        LOCAL
            .iter()
            .map(|(n, h, d)| (n.to_string(), h.to_string(), d.to_string())),
    );
    list
}

/// What a mode allows, as this UI does it: `/decide` and `/resume` run in this terminal.
pub fn describe(mode: Mode) -> String {
    match mode {
        Mode::Status => mode.description().to_string(),
        Mode::Design => "Also start and advance runs, write unsigned approval documents, sign a \
                         decision or resume a run in this terminal, and draft and confirm \
                         changes to the model registry."
            .to_string(),
    }
}

pub const KEYS: &str = "\
- `Enter` sends; `Alt+Enter` or `Ctrl+J` starts a new line.
- `Tab` completes a command; `Up` and `Down` at the edges of the input walk what you sent.
- `Esc` or `Ctrl+C` cancels the running turn: the A2A task is cancelled, which stops the run after \
its current step. `Ctrl+C` again leaves at once.
- `Ctrl+D` on an empty line leaves; `Ctrl+L` redraws.
- `PageUp`, `PageDown`, `Ctrl+Home` and `Ctrl+End` scroll the full-screen view.";

impl App {
    pub fn new(mode: Mode, theme: Theme) -> Self {
        App {
            items: Vec::new(),
            emitted: 0,
            stages: Vec::new(),
            input: make_input(&[String::new()], theme),
            history: Vec::new(),
            history_at: None,
            draft: String::new(),
            mode,
            run: None,
            running: None,
            question: None,
            up: 0,
            total: 0,
            quit: false,
            theme,
            streaming: false,
        }
    }

    pub fn blocks(&self) -> impl Iterator<Item = &Block> {
        self.items.iter().map(|i| &i.block)
    }

    pub fn notice(&mut self, text: impl Into<String>) {
        self.push(Block::Notice(text.into()));
    }

    fn push(&mut self, block: Block) {
        self.items.push(Item {
            block,
            rev: 0,
            cache: None,
        });
    }

    pub fn busy(&self) -> bool {
        self.running.is_some()
    }

    /// A turn is about to run on a worker thread; `cancel` stops it.
    pub fn turn_started(&mut self, cancel: Box<dyn Fn() + Send>) {
        self.running = Some(Running {
            started: Instant::now(),
            cancelling: false,
            cancel,
        });
        self.streaming = true;
        self.up = 0;
    }

    fn cancel(&mut self) {
        if let Some(running) = self.running.as_mut()
            && !running.cancelling
        {
            running.cancelling = true;
            (running.cancel)();
            self.notice(
                "Cancelling: the A2A task is cancelled, which stops the run after its current \
                 step. Nothing is undone.",
            );
        }
    }

    // ----- messages from a turn -----

    pub fn on_msg(&mut self, msg: Msg) {
        match msg {
            Msg::Update(update) => self.apply(*update),
            Msg::Bound(run) => {
                if self.run.as_deref() != Some(run.as_str()) {
                    self.stages.clear();
                }
                self.run = Some(run);
            }
            Msg::Ask { title, reply } => self.question = Some(Question { title, reply }),
            Msg::Foreground { .. } => {} // the loop answers it: it owns the terminal
            Msg::Done { stop, failed: _ } => {
                self.running = None;
                self.streaming = false;
                if stop == StopReason::Cancelled {
                    self.notice("Cancelled. `/status` shows where the run is.");
                }
            }
        }
    }

    pub fn apply(&mut self, update: SessionUpdate) {
        match update {
            SessionUpdate::AgentMessageChunk(chunk) => {
                let text = match chunk.content {
                    ContentBlock::Text(t) => t.text,
                    ContentBlock::ResourceLink(link) => {
                        let target = link.uri;
                        format!("[{}]({target})", link.name)
                    }
                    _ => return,
                };
                self.append_agent(&text);
            }
            SessionUpdate::Plan(plan) => {
                self.stages = plan
                    .entries
                    .into_iter()
                    .map(|e| Stage {
                        name: e.content,
                        state: match e.status {
                            PlanEntryStatus::Completed => StageState::Done,
                            PlanEntryStatus::InProgress => StageState::Working,
                            _ => StageState::Pending,
                        },
                    })
                    .collect();
            }
            SessionUpdate::ToolCall(call) => {
                self.push(Block::Tool {
                    id: call.tool_call_id.0.to_string(),
                    title: call.title,
                    state: tool_state(call.status),
                });
            }
            SessionUpdate::ToolCallUpdate(update) => {
                let id = update.tool_call_id.0.to_string();
                let item = self
                    .items
                    .iter_mut()
                    .rev()
                    .find(|i| matches!(&i.block, Block::Tool { id: tid, .. } if *tid == id));
                if let Some(item) = item
                    && let Block::Tool { title, state, .. } = &mut item.block
                {
                    if let Some(status) = update.fields.status {
                        *state = tool_state(status);
                    }
                    if let Some(new) = update.fields.title {
                        *title = new;
                    }
                    item.rev += 1;
                }
            }
            _ => {}
        }
    }

    fn append_agent(&mut self, text: &str) {
        if let Some(item) = self.items.last_mut()
            && let Block::Agent(md) = &mut item.block
        {
            md.push_str(text);
            item.rev += 1;
            return;
        }
        self.push(Block::Agent(text.to_string()));
    }

    // ----- keys -----

    /// What a key does. A permission question takes the key first.
    pub fn on_key(&mut self, key: KeyEvent) -> Option<Action> {
        if key.kind == KeyEventKind::Release {
            return None;
        }
        let ctrl = key.modifiers.contains(KeyModifiers::CONTROL);
        let alt = key.modifiers.contains(KeyModifiers::ALT);
        let shift = key.modifiers.contains(KeyModifiers::SHIFT);
        if let Some(question) = self.question.take() {
            let allow = matches!(key.code, KeyCode::Char('y' | 'Y')) && !ctrl;
            let deny = matches!(
                key.code,
                KeyCode::Char('n' | 'N') | KeyCode::Esc | KeyCode::Enter
            ) || (ctrl && key.code == KeyCode::Char('c'));
            if allow || deny {
                let _ = question.reply.send(allow);
                // A refusal is answered by the turn itself ("Not written."), once.
                if allow {
                    self.notice("Allowed: the document is written once.");
                }
            } else {
                self.question = Some(question);
            }
            return None;
        }
        match key.code {
            KeyCode::Char('c') if ctrl => {
                match &self.running {
                    Some(running) if running.cancelling => self.quit = true,
                    Some(_) => self.cancel(),
                    None if self.text().is_empty() => self.quit = true,
                    None => self.set_input(""),
                }
                None
            }
            KeyCode::Char('d') if ctrl => {
                if self.text().is_empty() && !self.busy() {
                    self.quit = true;
                }
                None
            }
            KeyCode::Char('l') if ctrl => Some(Action::Redraw),
            KeyCode::Esc => {
                if self.busy() {
                    self.cancel();
                } else if !self.completions().is_empty() {
                    self.set_input("");
                }
                None
            }
            KeyCode::Enter if alt || shift => {
                self.input.insert_newline();
                None
            }
            KeyCode::Char('j') if ctrl => {
                self.input.insert_newline();
                None
            }
            KeyCode::Enter => self.submit(),
            KeyCode::Tab => {
                self.complete();
                None
            }
            KeyCode::PageUp => {
                self.up = self.up.saturating_add(10);
                None
            }
            KeyCode::PageDown => {
                self.up = self.up.saturating_sub(10);
                None
            }
            KeyCode::Home if ctrl => {
                self.up = usize::MAX;
                None
            }
            KeyCode::End if ctrl => {
                self.up = 0;
                None
            }
            KeyCode::Up if self.input.cursor().0 == 0 && !alt => {
                self.history_prev();
                None
            }
            KeyCode::Down if self.input.cursor().0 + 1 >= self.input.lines().len() && !alt => {
                self.history_next();
                None
            }
            _ => {
                self.input.input(key);
                None
            }
        }
    }

    pub fn on_paste(&mut self, text: &str) {
        if self.question.is_none() {
            self.input
                .insert_str(text.replace("\r\n", "\n").replace('\r', "\n"));
        }
    }

    pub fn text(&self) -> String {
        self.input.lines().join("\n")
    }

    fn set_input(&mut self, text: &str) {
        let lines: Vec<String> = text.split('\n').map(str::to_string).collect();
        self.input = make_input(&lines, self.theme);
        self.history_at = None;
    }

    fn history_prev(&mut self) {
        if self.history.is_empty() {
            return;
        }
        let at = match self.history_at {
            None => {
                self.draft = self.text();
                self.history.len() - 1
            }
            Some(0) => 0,
            Some(i) => i - 1,
        };
        let text = self.history[at].clone();
        self.set_input(&text);
        self.history_at = Some(at);
    }

    fn history_next(&mut self) {
        let Some(at) = self.history_at else { return };
        if at + 1 < self.history.len() {
            let text = self.history[at + 1].clone();
            self.set_input(&text);
            self.history_at = Some(at + 1);
        } else {
            let draft = std::mem::take(&mut self.draft);
            self.set_input(&draft);
        }
    }

    // ----- sending -----

    fn submit(&mut self) -> Option<Action> {
        let text = self.text();
        let trimmed = text.trim();
        if trimmed.is_empty() {
            return None;
        }
        if self.busy() {
            self.notice("A turn is running: `Esc` cancels it, then send this again.");
            return None;
        }
        let trimmed = trimmed.to_string();
        if self.history.last() != Some(&trimmed) {
            self.history.push(trimmed.clone());
        }
        self.set_input("");
        self.push(Block::User(trimmed.clone()));
        let mut words = trimmed.split_whitespace();
        match (words.next(), words.next(), words.next()) {
            (Some("/quit" | "/exit"), None, _) => {
                self.quit = true;
                None
            }
            (Some("/clear"), None, _) => {
                self.items.clear();
                self.emitted = 0;
                self.up = 0;
                None
            }
            (Some("/help" | "help" | "?"), None, _) => {
                self.notice(self.help());
                None
            }
            (Some("/mode"), arg, None) => {
                self.set_mode(arg);
                None
            }
            _ => Some(Action::Run {
                text: trimmed,
                mode: self.mode,
                run: self.run.clone(),
            }),
        }
    }

    fn set_mode(&mut self, arg: Option<&str>) {
        match arg.map(|a| (a, Mode::from_id(a))) {
            None => self.notice(format!(
                "Mode: {}. {}",
                self.mode.name(),
                describe(self.mode)
            )),
            Some((_, Some(mode))) => {
                self.mode = mode;
                self.notice(format!("Mode: {}. {}", mode.name(), describe(mode)));
            }
            Some((other, None)) => self.notice(format!(
                "`{other}` is not a mode; the modes are `status` and `design`."
            )),
        }
    }

    pub fn help(&self) -> String {
        let mut text = String::from(
            "ln-agents drives the ln-pipeline orchestrator. It runs these commands as code. \
             Anything else you type is a question for the coordinator model: it answers in text \
             from the run's Markdown views, and cannot start, advance, sign or resume anything.\n\n",
        );
        for (name, hint, description) in commands_for(self.mode) {
            let usage = if hint.is_empty() {
                format!("/{name}")
            } else {
                format!("/{name} {hint}")
            };
            text.push_str(&format!("- `{usage}`: {description}\n"));
        }
        text.push_str(&format!(
            "\nMode: {}. {}\n\nNothing here approves for you. `/decide` and `/resume` leave this \
             screen for the same terminal, where you check what you sign, type the run id and your \
             SSH passphrase; ln-agents never sees either. `/approve`, `/sign` and `/reject` are not \
             commands. `/confirm <id>` writes only a registry change that a draft showed you, and \
             only when you type it.\n\nKeys:\n\n{KEYS}\n",
            self.mode.name(),
            describe(self.mode),
        ));
        text
    }

    // ----- completion -----

    /// The commands whose names begin with what is typed after the slash.
    pub fn completions(&self) -> Vec<(String, String, String)> {
        let text = self.text();
        let Some(prefix) = text.strip_prefix('/') else {
            return Vec::new();
        };
        if prefix.contains(char::is_whitespace) {
            return Vec::new();
        }
        commands_for(self.mode)
            .into_iter()
            .filter(|(name, _, _)| name.starts_with(prefix))
            .collect()
    }

    fn complete(&mut self) {
        let matches = self.completions();
        let Some(first) = matches.first() else { return };
        let typed = self.text();
        let typed = typed.trim_start_matches('/');
        let common = matches
            .iter()
            .map(|(n, _, _)| n.as_str())
            .fold(first.0.as_str(), |acc, n| {
                let len = acc
                    .chars()
                    .zip(n.chars())
                    .take_while(|(a, b)| a == b)
                    .count();
                &acc[..acc.char_indices().nth(len).map_or(acc.len(), |(i, _)| i)]
            })
            .to_string();
        if matches.len() == 1 {
            self.set_input(&format!("/{} ", matches[0].0));
        } else if common.len() > typed.len() {
            self.set_input(&format!("/{common}"));
        }
    }

    // ----- drawing support -----

    /// Every item as lines of at most `width` columns, a blank line between items.
    pub fn transcript(&mut self, width: u16) -> Vec<Line<'static>> {
        let theme = self.theme;
        let mut out: Vec<Line<'static>> = Vec::new();
        for item in &mut self.items {
            if !out.is_empty() {
                out.push(Line::default());
            }
            out.extend(item.lines(width, theme));
        }
        out
    }

    /// The items that are complete and not yet written into the terminal's scrollback, as lines.
    /// An item that may still grow (the message being streamed, a model call that has not
    /// returned) holds back everything after it, so the order is kept.
    pub fn take_ready(&mut self, width: u16) -> Vec<Line<'static>> {
        let theme = self.theme;
        let mut out = Vec::new();
        while self.emitted < self.items.len() {
            let i = self.emitted;
            let last = i + 1 == self.items.len();
            let ready = match &self.items[i].block {
                Block::Agent(_) => !last || !self.streaming,
                Block::Tool { state, .. } => *state != ToolState::Running,
                _ => true,
            };
            if !ready {
                break;
            }
            if i > 0 {
                out.push(Line::default());
            }
            out.extend(self.items[i].lines(width, theme));
            self.emitted += 1;
        }
        out
    }

    /// The part of the transcript still held back, for the inline screen's live rows.
    pub fn pending_tail(&mut self, width: u16, rows: usize) -> Vec<Line<'static>> {
        let theme = self.theme;
        let mut lines: Vec<Line<'static>> = Vec::new();
        for i in self.emitted..self.items.len() {
            lines.extend(self.items[i].lines(width, theme));
        }
        let skip = lines.len().saturating_sub(rows);
        lines.split_off(skip)
    }
}

impl Item {
    fn lines(&mut self, width: u16, theme: Theme) -> Vec<Line<'static>> {
        if let Some((w, rev, lines)) = &self.cache
            && *w == width
            && *rev == self.rev
        {
            return lines.clone();
        }
        let lines = match &self.block {
            Block::User(text) => {
                let mut lines = markdown::wrap_plain(
                    text,
                    width.saturating_sub(2),
                    Style::new().add_modifier(Modifier::BOLD),
                );
                for (i, line) in lines.iter_mut().enumerate() {
                    let lead = if i == 0 { "› " } else { "  " };
                    line.spans.insert(0, Span::styled(lead, theme.dim()));
                }
                lines
            }
            Block::Agent(md) => markdown::render(md, width, theme),
            Block::Tool { title, state, .. } => {
                let (mark, style) = match state {
                    ToolState::Running => ("…", theme.dim()),
                    ToolState::Done => ("✔", theme.dim()),
                    ToolState::Failed => ("✘", Style::new().add_modifier(Modifier::BOLD)),
                };
                let room = usize::from(width).saturating_sub(4);
                let first = title.lines().next().unwrap_or("");
                let mut shown: String = first.chars().take(room).collect();
                if first.chars().count() > room && room > 0 {
                    shown.pop();
                    shown.push('…');
                }
                vec![Line::from(vec![
                    Span::styled(format!("  {mark} "), style),
                    Span::styled(shown, style),
                ])]
            }
            Block::Notice(text) => markdown::render(text, width, theme)
                .into_iter()
                .map(|mut line| {
                    for span in &mut line.spans {
                        span.style = span.style.add_modifier(Modifier::ITALIC);
                    }
                    line
                })
                .collect(),
        };
        self.cache = Some((width, self.rev, lines.clone()));
        lines
    }
}

fn tool_state(status: ToolCallStatus) -> ToolState {
    match status {
        ToolCallStatus::Completed => ToolState::Done,
        ToolCallStatus::Failed => ToolState::Failed,
        _ => ToolState::Running,
    }
}

/// An input holding `lines`, the cursor at its end.
pub fn make_input(lines: &[String], theme: Theme) -> TextArea<'static> {
    let mut input = TextArea::new(lines.to_vec());
    input.set_wrap_mode(WrapMode::Word);
    input.set_max_rows(INPUT_ROWS);
    input.set_cursor_line_style(Style::new());
    input.set_placeholder_text("A command (/help) or a question for the coordinator");
    input.set_placeholder_style(theme.dim());
    input.set_block(Frame::bordered().border_style(theme.dim()));
    input.move_cursor(CursorMove::Bottom);
    input.move_cursor(CursorMove::End);
    input
}

#[cfg(test)]
mod tests {
    use std::sync::Arc;
    use std::sync::atomic::{AtomicUsize, Ordering};
    use std::sync::mpsc::channel;

    use agent_client_protocol_schema::v1::{
        ContentChunk, Plan, PlanEntry, PlanEntryPriority, ToolCall, ToolCallId, ToolCallUpdate,
        ToolCallUpdateFields,
    };

    use super::*;

    fn app() -> App {
        App::new(Mode::Status, Theme::new(false))
    }

    fn key(code: KeyCode) -> KeyEvent {
        KeyEvent::new(code, KeyModifiers::NONE)
    }

    fn ctrl(c: char) -> KeyEvent {
        KeyEvent::new(KeyCode::Char(c), KeyModifiers::CONTROL)
    }

    fn type_text(app: &mut App, text: &str) {
        for c in text.chars() {
            app.on_key(key(KeyCode::Char(c)));
        }
    }

    fn say(text: &str) -> SessionUpdate {
        SessionUpdate::AgentMessageChunk(ContentChunk::new(text.to_string().into()))
    }

    fn lines_text(lines: &[Line<'_>]) -> Vec<String> {
        lines
            .iter()
            .map(|l| l.spans.iter().map(|s| s.content.as_ref()).collect())
            .collect()
    }

    #[test]
    fn enter_sends_the_text_as_a_turn_with_the_mode_and_the_bound_run() {
        let mut app = app();
        app.on_msg(Msg::Bound("r1".into()));
        type_text(&mut app, "/status");
        let action = app.on_key(key(KeyCode::Enter));
        assert_eq!(
            action,
            Some(Action::Run {
                text: "/status".into(),
                mode: Mode::Status,
                run: Some("r1".into())
            })
        );
        assert_eq!(app.text(), "");
        assert!(matches!(app.blocks().last(), Some(Block::User(t)) if t == "/status"));
    }

    #[test]
    fn alt_enter_and_ctrl_j_start_a_new_line_instead_of_sending() {
        let mut app = app();
        type_text(&mut app, "a");
        assert_eq!(
            app.on_key(KeyEvent::new(KeyCode::Enter, KeyModifiers::ALT)),
            None
        );
        type_text(&mut app, "b");
        assert_eq!(app.on_key(ctrl('j')), None);
        type_text(&mut app, "c");
        assert_eq!(app.text(), "a\nb\nc");
    }

    #[test]
    fn empty_input_sends_nothing() {
        let mut app = app();
        assert_eq!(app.on_key(key(KeyCode::Enter)), None);
        assert_eq!(app.blocks().count(), 0);
    }

    #[test]
    fn the_ui_answers_its_own_commands_without_a_turn() {
        let mut app = app();
        for line in ["/clear", "/quit"] {
            type_text(&mut app, line);
            assert_eq!(app.on_key(key(KeyCode::Enter)), None, "{line}");
        }
        assert!(app.quit);

        let mut app = self::app();
        type_text(&mut app, "/mode design");
        app.on_key(key(KeyCode::Enter));
        assert_eq!(app.mode, Mode::Design);
        type_text(&mut app, "/mode nonsense");
        app.on_key(key(KeyCode::Enter));
        assert_eq!(app.mode, Mode::Design);
        assert!(matches!(app.blocks().last(), Some(Block::Notice(t)) if t.contains("not a mode")));
    }

    #[test]
    fn help_names_the_keys_and_says_signing_happens_in_this_terminal() {
        let help = app().help();
        assert!(help.contains("`Alt+Enter`"), "{help}");
        assert!(
            help.contains("leave this screen for the same terminal"),
            "{help}"
        );
        assert!(!help.contains("opens a terminal window"), "{help}");
        assert!(!help.contains("open the terminal where"), "{help}");
    }

    #[test]
    fn the_design_mode_text_says_signing_happens_in_this_terminal() {
        let mut app = app();
        app.mode = Mode::Design;
        let help = app.help();
        assert!(help.contains("in this terminal"), "{help}");
        assert!(!help.contains("open the terminal where"), "{help}");
        type_text(&mut app, "/mode");
        app.on_key(key(KeyCode::Enter));
        assert!(matches!(
            app.blocks().last(),
            Some(Block::Notice(t)) if t.contains("in this terminal")
        ));
    }

    #[test]
    fn the_decide_and_resume_lines_say_this_terminal() {
        let list = commands_for(Mode::Design);
        let decide = list.iter().find(|c| c.0 == "decide").unwrap();
        assert!(decide.2.contains("here"), "{decide:?}");
        assert!(list.iter().any(|c| c.0 == "quit"));
    }

    #[test]
    fn a_second_turn_is_refused_while_one_runs() {
        let mut app = app();
        app.turn_started(Box::new(|| {}));
        type_text(&mut app, "/status");
        assert_eq!(app.on_key(key(KeyCode::Enter)), None);
        assert!(matches!(app.blocks().last(), Some(Block::Notice(t)) if t.contains("Esc")));
    }

    #[test]
    fn esc_cancels_a_running_turn_once_and_says_what_that_means() {
        let mut app = app();
        let calls = Arc::new(AtomicUsize::new(0));
        let seen = Arc::clone(&calls);
        app.turn_started(Box::new(move || {
            seen.fetch_add(1, Ordering::SeqCst);
        }));
        app.on_key(key(KeyCode::Esc));
        app.on_key(key(KeyCode::Esc));
        assert_eq!(calls.load(Ordering::SeqCst), 1);
        assert!(!app.quit);
        let notice = app.blocks().last().unwrap();
        assert!(
            matches!(notice, Block::Notice(t) if t.contains("stops the run after its current step")),
            "{notice:?}"
        );
        // a second Ctrl+C while cancelling leaves at once
        app.on_key(ctrl('c'));
        assert!(app.quit);
    }

    #[test]
    fn ctrl_c_clears_a_typed_line_then_leaves() {
        let mut app = app();
        type_text(&mut app, "abc");
        app.on_key(ctrl('c'));
        assert_eq!(app.text(), "");
        assert!(!app.quit);
        app.on_key(ctrl('c'));
        assert!(app.quit);
    }

    #[test]
    fn ctrl_d_leaves_only_from_an_empty_line() {
        let mut app = app();
        type_text(&mut app, "x");
        app.on_key(ctrl('d'));
        assert!(!app.quit);
        app.on_key(key(KeyCode::Backspace));
        app.on_key(ctrl('d'));
        assert!(app.quit);
    }

    #[test]
    fn a_released_key_does_nothing() {
        let mut app = app();
        let mut release = key(KeyCode::Char('x'));
        release.kind = KeyEventKind::Release;
        app.on_key(release);
        assert_eq!(app.text(), "");
    }

    #[test]
    fn up_and_down_walk_what_was_sent_and_restore_the_draft() {
        let mut app = app();
        for line in ["/runs", "/models"] {
            type_text(&mut app, line);
            app.on_key(key(KeyCode::Enter));
            app.on_msg(Msg::Done {
                stop: StopReason::EndTurn,
                failed: false,
            });
        }
        type_text(&mut app, "draft");
        app.on_key(key(KeyCode::Up));
        assert_eq!(app.text(), "/models");
        app.on_key(key(KeyCode::Up));
        assert_eq!(app.text(), "/runs");
        app.on_key(key(KeyCode::Down));
        assert_eq!(app.text(), "/models");
        app.on_key(key(KeyCode::Down));
        assert_eq!(app.text(), "draft");
    }

    #[test]
    fn tab_completes_a_unique_command_and_the_common_prefix_of_several() {
        let mut app = app();
        type_text(&mut app, "/gui");
        app.on_key(key(KeyCode::Tab));
        assert_eq!(app.text(), "/guideline ");

        let mut app = self::app();
        type_text(&mut app, "/m");
        let names: Vec<String> = app.completions().into_iter().map(|c| c.0).collect();
        assert_eq!(names, vec!["models", "mode"]);
        app.on_key(key(KeyCode::Tab));
        assert_eq!(app.text(), "/mode", "the common prefix of models and mode");
    }

    #[test]
    fn design_commands_are_offered_only_in_design_mode() {
        let mut app = app();
        type_text(&mut app, "/adv");
        assert!(app.completions().is_empty());
        app.mode = Mode::Design;
        assert_eq!(app.completions().len(), 1);
    }

    #[test]
    fn message_chunks_join_into_one_agent_block_until_something_else_comes() {
        let mut app = app();
        app.apply(say("Run `r`: "));
        app.apply(say("advancing.\n"));
        app.apply(SessionUpdate::ToolCall(
            ToolCall::new(ToolCallId::new("c1"), "model call").status(ToolCallStatus::InProgress),
        ));
        app.apply(say("after"));
        let blocks: Vec<&Block> = app.blocks().collect();
        assert!(matches!(blocks[0], Block::Agent(t) if t == "Run `r`: advancing.\n"));
        assert!(matches!(
            blocks[1],
            Block::Tool {
                state: ToolState::Running,
                ..
            }
        ));
        assert!(matches!(blocks[2], Block::Agent(t) if t == "after"));
    }

    #[test]
    fn a_tool_call_update_changes_its_state_and_title() {
        let mut app = app();
        app.apply(SessionUpdate::ToolCall(
            ToolCall::new(ToolCallId::new("c1"), "calling").status(ToolCallStatus::InProgress),
        ));
        app.apply(SessionUpdate::ToolCallUpdate(ToolCallUpdate::new(
            ToolCallId::new("c1"),
            ToolCallUpdateFields::new()
                .status(ToolCallStatus::Completed)
                .title("done".to_string()),
        )));
        assert!(matches!(
            app.blocks().next(),
            Some(Block::Tool { title, state: ToolState::Done, .. }) if title == "done"
        ));
    }

    #[test]
    fn the_plan_becomes_the_stages_and_a_new_run_clears_them() {
        let mut app = app();
        let entry = |name: &str, status| {
            PlanEntry::new(name.to_string(), PlanEntryPriority::Medium, status)
        };
        app.on_msg(Msg::Bound("r1".into()));
        app.apply(SessionUpdate::Plan(Plan::new(vec![
            entry("G", PlanEntryStatus::Completed),
            entry("A1", PlanEntryStatus::InProgress),
        ])));
        assert_eq!(
            app.stages,
            vec![
                Stage {
                    name: "G".into(),
                    state: StageState::Done
                },
                Stage {
                    name: "A1".into(),
                    state: StageState::Working
                },
            ]
        );
        app.on_msg(Msg::Bound("r1".into()));
        assert_eq!(app.stages.len(), 2);
        app.on_msg(Msg::Bound("r2".into()));
        assert!(app.stages.is_empty());
    }

    #[test]
    fn a_permission_question_takes_the_next_key_and_defaults_to_no() {
        let mut app = app();
        let (reply, answer) = channel();
        app.on_msg(Msg::Ask {
            title: "Write?".into(),
            reply,
        });
        app.on_key(key(KeyCode::Char('x')));
        assert!(app.question.is_some(), "an unrelated key keeps asking");
        app.on_key(key(KeyCode::Enter));
        assert!(!answer.recv().unwrap());
        assert!(app.question.is_none());

        let (reply, answer) = channel();
        app.on_msg(Msg::Ask {
            title: "Write?".into(),
            reply,
        });
        app.on_key(key(KeyCode::Char('y')));
        assert!(answer.recv().unwrap());
    }

    #[test]
    fn inline_output_waits_for_the_streaming_message_and_keeps_order() {
        let mut app = app();
        app.turn_started(Box::new(|| {}));
        app.apply(say("# Title\n\nbody"));
        assert!(app.take_ready(40).is_empty(), "the message may still grow");
        app.apply(SessionUpdate::ToolCall(
            ToolCall::new(ToolCallId::new("c1"), "calling").status(ToolCallStatus::InProgress),
        ));
        let first = app.take_ready(40);
        assert_eq!(lines_text(&first), vec!["Title", "", "body"]);
        assert!(
            app.take_ready(40).is_empty(),
            "the model call has not returned"
        );
        app.apply(SessionUpdate::ToolCallUpdate(ToolCallUpdate::new(
            ToolCallId::new("c1"),
            ToolCallUpdateFields::new().status(ToolCallStatus::Completed),
        )));
        let second = lines_text(&app.take_ready(40));
        assert_eq!(second, vec!["", "  ✔ calling"]);
        app.apply(say("end"));
        assert!(app.take_ready(40).is_empty());
        app.on_msg(Msg::Done {
            stop: StopReason::EndTurn,
            failed: false,
        });
        assert_eq!(lines_text(&app.take_ready(40)), vec!["", "end"]);
    }

    #[test]
    fn the_transcript_puts_a_blank_line_between_items() {
        let mut app = app();
        type_text(&mut app, "hi");
        app.on_key(key(KeyCode::Enter));
        app.apply(say("hello"));
        assert_eq!(lines_text(&app.transcript(40)), vec!["› hi", "", "hello"]);
    }

    #[test]
    fn a_long_user_line_wraps_under_its_marker() {
        let mut app = app();
        type_text(&mut app, "aaaa bbbb cccc");
        app.on_key(key(KeyCode::Enter));
        assert_eq!(
            lines_text(&app.transcript(10)),
            vec!["› aaaa ", "  bbbb ", "  cccc"]
        );
    }

    #[test]
    fn a_paste_goes_into_the_input_and_normalizes_line_ends() {
        let mut app = app();
        app.on_paste("a\r\nb");
        assert_eq!(app.text(), "a\nb");
    }

    #[test]
    fn a_cancelled_turn_says_so() {
        let mut app = app();
        app.turn_started(Box::new(|| {}));
        app.on_msg(Msg::Done {
            stop: StopReason::Cancelled,
            failed: false,
        });
        assert!(!app.busy());
        assert!(
            matches!(app.blocks().last(), Some(Block::Notice(t)) if t.starts_with("Cancelled"))
        );
    }
}
