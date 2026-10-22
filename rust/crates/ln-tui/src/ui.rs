//! Drawing: the transcript, the stages of the run being driven, the input and the status line.
//! The full-screen layout and the inline one share every piece below the transcript.

use std::time::{Duration, Instant};

use ratatui::backend::Backend;
use ratatui::layout::{Constraint, Layout, Rect};
use ratatui::style::{Color, Modifier, Style};
use ratatui::text::{Line, Span, Text};
use ratatui::widgets::{Block, Paragraph, Widget};
use ratatui::{Frame, Terminal};

use crate::app::{App, Hits, INPUT_ROWS, StageState};
use crate::markdown::columns;
use crate::term::Screen;

const SPINNER: [&str; 10] = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"];

/// Draw the screen. On the inline screen the messages that are complete are first written into
/// the terminal's scrollback, above the viewport.
pub fn render<B: Backend>(
    terminal: &mut Terminal<B>,
    app: &mut App,
    screen: Screen,
) -> Result<(), B::Error> {
    if screen == Screen::Inline {
        let width = terminal.size()?.width;
        let ready = app.take_ready(width);
        insert_above(terminal, ready)?;
    }
    terminal.draw(|frame| draw(frame, app, screen, Instant::now()))?;
    Ok(())
}

/// Write finished lines into the terminal's scrollback, above the inline viewport.
fn insert_above<B: Backend>(
    terminal: &mut Terminal<B>,
    lines: Vec<Line<'static>>,
) -> Result<(), B::Error> {
    for chunk in lines.chunks(200) {
        let height = u16::try_from(chunk.len()).unwrap_or(u16::MAX);
        let text = Text::from(chunk.to_vec());
        terminal.insert_before(height, |buf| {
            Paragraph::new(text).render(buf.area, buf);
        })?;
    }
    Ok(())
}

/// Draw one frame.
pub fn draw(frame: &mut Frame, app: &mut App, screen: Screen, now: Instant) {
    let area = frame.area();
    if area.width < 20 || area.height < 5 {
        frame.render_widget(Paragraph::new("too small"), area);
        return;
    }
    app.hits = Hits::default();
    let input_rows = if let Some(pick) = &app.pick {
        picker_rows(pick.options.len(), area.height, screen)
    } else if app.question.is_some() {
        4 // the question, how to answer it, and the border
    } else {
        app.input
            .measure(area.width)
            .preferred_rows
            .clamp(3, INPUT_ROWS)
    };
    let [top, hint, input, status] = Layout::vertical([
        Constraint::Min(0),
        Constraint::Length(1),
        Constraint::Length(input_rows),
        Constraint::Length(1),
    ])
    .areas(area);
    match screen {
        Screen::Alternate => transcript(frame, app, top),
        Screen::Inline => live_tail(frame, app, top),
    }
    let (hint_text, named) = hint_line(app, area.width, now);
    frame.render_widget(Paragraph::new(hint_text), hint);
    app.hits.commands = named
        .into_iter()
        .map(|(from, to, name)| {
            let rect = Rect {
                x: hint.x + from,
                y: hint.y,
                width: to - from,
                height: 1,
            };
            (rect, name)
        })
        .collect();
    if app.pick.is_some() {
        picker(frame, app, input, screen);
        frame.render_widget(Paragraph::new(status_line(app, area.width)), status);
        return;
    }
    match &app.question {
        Some(question) => {
            let text = Text::from(vec![
                Line::from(Span::styled(
                    question.title.clone(),
                    Style::new().add_modifier(Modifier::BOLD),
                )),
                Line::from("y writes it, once. n or Enter does not (the default)."),
            ]);
            frame.render_widget(
                Paragraph::new(text).block(Block::bordered().border_style(app.theme.dim())),
                input,
            );
        }
        None => frame.render_widget(&app.input, input),
    }
    frame.render_widget(Paragraph::new(status_line(app, area.width)), status);
}

/// The full-screen transcript, scrolled by `app.up` lines from the bottom.
fn transcript(frame: &mut Frame, app: &mut App, area: Rect) {
    let lines = app.transcript(area.width);
    let total = lines.len();
    let height = usize::from(area.height);
    if let Some(item) = app.take_focus() {
        // show that item from its first line (or as far up as the end of the transcript allows)
        let first = app.offset_of(item, area.width);
        app.up = total.saturating_sub(height + first);
        app.total = total;
    }
    if app.up > 0 && total > app.total {
        app.up = app.up.saturating_add(total - app.total); // keep what is being read where it is
    }
    app.total = total;
    let max_up = total.saturating_sub(height);
    app.up = app.up.min(max_up);
    let start = total.saturating_sub(height + app.up);
    let visible: Vec<Line<'static>> = lines.into_iter().skip(start).take(height).collect();
    frame.render_widget(Paragraph::new(Text::from(visible)), area);
    if app.up > 0 {
        let note = format!(" ↓ {} more lines · Ctrl+End ", app.up);
        let width = u16::try_from(columns(&note))
            .unwrap_or(u16::MAX)
            .min(area.width);
        let at = Rect {
            x: area.x + area.width - width,
            y: area.y + area.height.saturating_sub(1),
            width,
            height: 1,
        };
        frame.render_widget(
            Paragraph::new(Span::styled(
                note,
                Style::new().add_modifier(Modifier::REVERSED),
            )),
            at,
        );
    }
}

/// The inline screen's rows above the hint line: the last lines of what is still being written.
fn live_tail(frame: &mut Frame, app: &mut App, area: Rect) {
    let rows = usize::from(area.height);
    let tail = app.pending_tail(area.width, rows);
    let blank = rows.saturating_sub(tail.len());
    let mut lines: Vec<Line<'static>> = vec![Line::default(); blank];
    lines.extend(tail);
    frame.render_widget(Paragraph::new(Text::from(lines)), area);
}

/// While typing a command, the commands that match; otherwise the stages of the run and how long
/// the turn has been going.
fn hint_line(app: &App, width: u16, now: Instant) -> (Line<'static>, Vec<(u16, u16, String)>) {
    let dim = app.theme.dim();
    if app.question.is_some() || app.pick.is_some() {
        return (Line::default(), Vec::new());
    }
    if app.line.is_some() {
        let note = "Enter writes the document · Esc cancels";
        return (Line::from(Span::styled(note, dim)), Vec::new());
    }
    let matches = app.completions();
    if !matches.is_empty() {
        let mut spans = Vec::new();
        let mut named = Vec::new();
        let mut x = 0usize;
        for (i, (name, hint, description)) in matches.iter().enumerate() {
            if i > 0 {
                spans.push(Span::styled("  ", dim));
                x += 2;
            }
            let label = format!("/{name}");
            let w = columns(&label);
            if x + w <= usize::from(width) {
                named.push((x as u16, (x + w) as u16, name.clone()));
            }
            x += w;
            spans.push(Span::styled(
                label,
                Style::new().add_modifier(Modifier::BOLD),
            ));
            if matches.len() == 1 {
                if !hint.is_empty() {
                    spans.push(Span::styled(format!(" {hint}"), dim));
                }
                spans.push(Span::styled(format!(" — {description}"), dim));
            }
        }
        return (fit(spans, width), named);
    }
    (progress_line(app, width, now), Vec::new())
}

/// The rows the picker takes: its options, the border and a line of keys. Full screen keeps a few
/// lines of the conversation in view; the inline screen has only its few rows, so the list scrolls.
fn picker_rows(options: usize, height: u16, screen: Screen) -> u16 {
    let want = u16::try_from(options).unwrap_or(u16::MAX).saturating_add(3);
    let room = match screen {
        Screen::Alternate => height.saturating_sub(5),
        Screen::Inline => height.saturating_sub(2),
    };
    want.min(room).max(4)
}

/// The picker over the input: `1. Approve Design A` and so on, the marked one bold, and a line
/// of the keys (and the mouse, where the full screen has it).
fn picker(frame: &mut Frame, app: &mut App, area: Rect, screen: Screen) {
    let dim = app.theme.dim();
    let color = app.theme.color();
    let Some(pick) = app.pick.as_mut() else {
        return;
    };
    let block = Block::bordered().border_style(dim).title(Span::styled(
        format!(" {} ", pick.title),
        Style::new().add_modifier(Modifier::BOLD),
    ));
    let inner = block.inner(area);
    frame.render_widget(block, area);
    let rows = usize::from(inner.height.saturating_sub(1));
    pick.keep_visible(rows);
    let mut options = Vec::new();
    let end = (pick.first + rows).min(pick.options.len());
    for (row, at) in (pick.first..end).enumerate() {
        let marked = at == pick.selected;
        let number = if at < 9 {
            format!("{}. ", at + 1)
        } else {
            "   ".to_string()
        };
        let style = if marked {
            fg(color, Color::Cyan, Modifier::BOLD)
        } else {
            Style::new()
        };
        let line = fit(
            vec![
                Span::styled(if marked { "❯ " } else { "  " }, style),
                Span::styled(number, style),
                Span::styled(pick.options[at].clone(), style),
            ],
            inner.width,
        );
        let rect = Rect {
            x: inner.x,
            y: inner.y + row as u16,
            width: inner.width,
            height: 1,
        };
        frame.render_widget(Paragraph::new(line), rect);
        options.push((rect, at));
    }
    let more = pick.options.len().saturating_sub(end);
    let mut keys = match pick.options.len() {
        0 | 1 => String::from("1 or Enter"),
        n => format!("1-{} or ↑↓ Enter", n.min(9)),
    };
    if screen == Screen::Alternate {
        keys.push_str(" · click");
    }
    if pick.has_report() {
        keys.push_str(" · r reads the report");
    }
    keys.push_str(" · Esc leaves");
    if pick.first > 0 {
        keys.push_str(" · ↑ more");
    }
    if more > 0 {
        keys.push_str(&format!(" · ↓ {more} more"));
    }
    let footer = Rect {
        x: inner.x,
        y: inner.y + inner.height.saturating_sub(1),
        width: inner.width,
        height: 1,
    };
    frame.render_widget(
        Paragraph::new(fit(
            vec![Span::styled(format!("  {keys}"), dim)],
            inner.width,
        )),
        footer,
    );
    app.hits.options = options;
    app.hits.picker = Some(area);
}

/// `⠋ r1  ■G ■A1 ▶A3  0:42  Esc cancels` — the stages the turn has entered, the last working.
pub fn progress_line(app: &App, width: u16, now: Instant) -> Line<'static> {
    let dim = app.theme.dim();
    let color = app.theme.color();
    let mut head: Vec<Span<'static>> = Vec::new();
    match &app.running {
        Some(running) => {
            let elapsed = now.saturating_duration_since(running.started);
            let frame = (elapsed.as_millis() / 100) as usize % SPINNER.len();
            head.push(Span::styled(
                format!("{} ", SPINNER[frame]),
                Style::new().add_modifier(Modifier::BOLD),
            ));
        }
        None => head.push(Span::styled("· ", dim)),
    }
    if let Some(run) = &app.run {
        head.push(Span::styled(
            format!("{run}  "),
            Style::new().add_modifier(Modifier::BOLD),
        ));
    }
    let mut tail: Vec<Span<'static>> = Vec::new();
    if let Some(running) = &app.running {
        let elapsed = now.saturating_duration_since(running.started);
        tail.push(Span::styled(format!("  {}", clock(elapsed)), dim));
        tail.push(Span::styled(
            if running.cancelling {
                "  cancelling…"
            } else {
                "  Esc cancels"
            },
            dim,
        ));
    }
    let used: usize = head.iter().chain(&tail).map(|s| columns(&s.content)).sum();
    let room = usize::from(width).saturating_sub(used);
    let marks: Vec<(String, Style)> = app
        .stages
        .iter()
        .map(|stage| {
            let (glyph, style) = match stage.state {
                StageState::Done => ("■", fg(color, Color::Green, Modifier::empty())),
                // working while a turn runs; once it has stopped, this is only where the run stopped:
                // why (a signature, a pause, a failure) is in the message above, not in this mark
                StageState::Working if app.running.is_some() => {
                    ("▶", fg(color, Color::Yellow, Modifier::BOLD))
                }
                StageState::Working => ("□", fg(color, Color::Yellow, Modifier::BOLD)),
                StageState::Pending => ("·", dim),
            };
            (format!("{glyph}{}", stage.name), style)
        })
        .collect();
    // Keep the newest stages that fit, with a mark where older ones were left out.
    let mut shown: Vec<&(String, Style)> = Vec::new();
    let mut cost = 0;
    for mark in marks.iter().rev() {
        let w = columns(&mark.0) + 1;
        if cost + w + 2 > room && !shown.is_empty() {
            break;
        }
        cost += w;
        shown.push(mark);
    }
    shown.reverse();
    let mut spans = head;
    if shown.len() < marks.len() {
        spans.push(Span::styled("… ", dim));
    }
    for (text, style) in shown {
        spans.push(Span::styled(text.clone(), *style));
        spans.push(Span::raw(" "));
    }
    spans.extend(tail);
    fit(spans, width)
}

fn fg(color: bool, c: Color, modifier: Modifier) -> Style {
    let style = Style::new().add_modifier(modifier);
    if color { style.fg(c) } else { style }
}

fn clock(elapsed: Duration) -> String {
    let secs = elapsed.as_secs();
    format!("{}:{:02}", secs / 60, secs % 60)
}

/// The spans cut to `width` columns.
fn fit(spans: Vec<Span<'static>>, width: u16) -> Line<'static> {
    let mut used = 0usize;
    let mut out = Vec::new();
    for span in spans {
        let w = columns(&span.content);
        if used + w <= usize::from(width) {
            used += w;
            out.push(span);
            continue;
        }
        let room = usize::from(width) - used;
        let cut: String = span
            .content
            .chars()
            .scan(0usize, |acc, c| {
                *acc += columns(&c.to_string());
                (*acc <= room).then_some(c)
            })
            .collect();
        out.push(Span::styled(cut, span.style));
        break;
    }
    Line::from(out)
}

fn status_line(app: &App, width: u16) -> Line<'static> {
    let dim = app.theme.dim();
    let left = format!(
        " {}{}",
        app.mode.name(),
        app.run
            .as_ref()
            .map(|r| format!(" · run {r}"))
            .unwrap_or_default()
    );
    let right = if app.busy() {
        "Esc cancels "
    } else {
        "/ commands · Ctrl+D quits "
    };
    let gap = usize::from(width)
        .saturating_sub(columns(&left) + columns(right))
        .max(1);
    fit(
        vec![
            Span::styled(left, dim.add_modifier(Modifier::BOLD)),
            Span::styled(" ".repeat(gap), dim),
            Span::styled(right.to_string(), dim),
        ],
        width,
    )
}

#[cfg(test)]
mod tests {
    use agent_client_protocol_schema::v1::{
        Plan, PlanEntry, PlanEntryPriority, PlanEntryStatus, SessionUpdate,
    };
    use crossterm::event::{MouseButton, MouseEvent, MouseEventKind};
    use ln_acp::commands::Mode;
    use ratatui::backend::TestBackend;
    use ratatui::{TerminalOptions, Viewport};

    use super::*;
    use crate::app::{Msg, make_input};
    use crate::markdown::Theme;

    fn app() -> App {
        let mut app = App::new(Mode::Status, Theme::new(false));
        app.input = make_input(&[String::new()], Theme::new(false));
        app
    }

    fn screen_text(terminal: &Terminal<TestBackend>) -> Vec<String> {
        let buffer = terminal.backend().buffer();
        (0..buffer.area.height)
            .map(|y| {
                (0..buffer.area.width)
                    .map(|x| buffer[(x, y)].symbol().to_string())
                    .collect::<String>()
                    .trim_end()
                    .to_string()
            })
            .collect()
    }

    fn frame_rows(app: &mut App, w: u16, h: u16, screen: Screen) -> Vec<String> {
        let mut terminal = Terminal::new(TestBackend::new(w, h)).unwrap();
        terminal
            .draw(|f| draw(f, app, screen, Instant::now()))
            .unwrap();
        screen_text(&terminal)
    }

    fn plan(entries: &[(&str, PlanEntryStatus)]) -> SessionUpdate {
        SessionUpdate::Plan(Plan::new(
            entries
                .iter()
                .map(|(n, s)| PlanEntry::new(n.to_string(), PlanEntryPriority::Medium, s.clone()))
                .collect(),
        ))
    }

    #[test]
    fn the_full_screen_shows_the_transcript_above_the_input_and_the_status() {
        let mut app = app();
        app.notice("Welcome to ln-agents");
        let rows = frame_rows(&mut app, 60, 12, Screen::Alternate);
        assert_eq!(rows[0], "Welcome to ln-agents");
        let last = rows.last().unwrap();
        assert!(last.contains("Status (read-only)"), "{last}");
        assert!(last.contains("Ctrl+D quits"), "{last}");
        assert!(rows.iter().any(|r| r.starts_with('┌')), "{rows:?}");
    }

    #[test]
    fn the_stage_a_run_stopped_at_is_marked_differently_from_one_still_working() {
        let mut app = app();
        app.apply(plan(&[
            ("G", PlanEntryStatus::Completed),
            ("H1", PlanEntryStatus::InProgress),
        ]));
        let idle = progress_line(&app, 60, Instant::now());
        let idle: String = idle.spans.iter().map(|s| s.content.as_ref()).collect();
        assert!(idle.contains("■G □H1"), "{idle}");
        app.turn_started(Box::new(|| {}));
        let busy = progress_line(&app, 60, Instant::now());
        let busy: String = busy.spans.iter().map(|s| s.content.as_ref()).collect();
        assert!(busy.contains("■G ▶H1"), "{busy}");
    }

    #[test]
    fn the_stages_of_the_run_are_drawn_with_a_mark_for_each_state() {
        let mut app = app();
        app.on_msg(Msg::Bound("r1".into()));
        app.apply(plan(&[
            ("G", PlanEntryStatus::Completed),
            ("A1", PlanEntryStatus::Completed),
            ("A2", PlanEntryStatus::InProgress),
        ]));
        app.turn_started(Box::new(|| {}));
        let rows = frame_rows(&mut app, 60, 12, Screen::Alternate);
        assert!(
            rows.iter()
                .any(|r| r.contains("r1") && r.contains("■G ■A1 ▶A2")),
            "{rows:?}"
        );
    }

    #[test]
    fn a_running_turn_shows_a_clock_and_how_to_cancel() {
        let mut app = app();
        app.turn_started(Box::new(|| {}));
        let rows = frame_rows(&mut app, 60, 12, Screen::Alternate);
        assert!(
            rows.iter()
                .any(|r| r.contains("0:00") && r.contains("Esc cancels")),
            "{rows:?}"
        );
    }

    #[test]
    fn too_many_stages_keep_the_newest_and_mark_the_rest() {
        let mut app = app();
        let names: Vec<String> = (1..=30).map(|i| format!("S{i}")).collect();
        let entries: Vec<(&str, PlanEntryStatus)> = names
            .iter()
            .map(|n| (n.as_str(), PlanEntryStatus::Completed))
            .collect();
        app.apply(plan(&entries));
        let line = progress_line(&app, 30, Instant::now());
        let text: String = line.spans.iter().map(|s| s.content.as_ref()).collect();
        assert!(text.contains('…'), "{text}");
        assert!(text.contains("■S30"), "{text}");
        assert!(!text.contains("■S1 "), "{text}");
        assert!(columns(&text) <= 30, "{text}");
    }

    #[test]
    fn typing_a_slash_lists_the_matching_commands() {
        let mut app = app();
        app.input.insert_str("/ev");
        let rows = frame_rows(&mut app, 100, 12, Screen::Alternate);
        assert!(
            rows.iter()
                .any(|r| r.contains("/evidence") && r.contains("[run]")),
            "{rows:?}"
        );
    }

    #[test]
    fn a_permission_question_replaces_the_input() {
        let mut app = app();
        let (reply, _answer) = std::sync::mpsc::channel();
        app.on_msg(Msg::Ask {
            title: "Write an unsigned design document for run r1?".into(),
            reply,
        });
        let rows = frame_rows(&mut app, 80, 12, Screen::Alternate);
        let all = rows.join("\n");
        assert!(all.contains("Write an unsigned design document"), "{all}");
        assert!(all.contains("n or Enter does not"), "{all}");
    }

    fn pick_msg(
        options: &[&str],
        report: Option<&str>,
    ) -> (Msg, std::sync::mpsc::Receiver<Option<usize>>) {
        let (reply, answer) = std::sync::mpsc::channel();
        let msg = Msg::Pick {
            title: "H1 · r1: what do you sign?".into(),
            options: options.iter().map(|o| o.to_string()).collect(),
            report: report.map(str::to_string),
            reply,
        };
        (msg, answer)
    }

    const DESIGNS: [&str; 3] = [
        "Approve Design A",
        "Approve Design B",
        "Approve the synthesis",
    ];

    #[test]
    fn a_picker_lists_numbered_options_and_the_keys() {
        let mut app = app();
        app.notice("Welcome");
        app.on_msg(pick_msg(&DESIGNS, Some("# Report")).0);
        let rows = frame_rows(&mut app, 80, 14, Screen::Alternate);
        let all = rows.join("\n");
        assert!(all.contains("H1 · r1: what do you sign?"), "{all}");
        assert!(all.contains("❯ 1. Approve Design A"), "{all}");
        assert!(all.contains("  2. Approve Design B"), "{all}");
        assert!(all.contains("3. Approve the synthesis"), "{all}");
        assert!(
            all.contains("click"),
            "the full screen takes the mouse: {all}"
        );
        assert!(all.contains("r reads the report"), "{all}");
        assert_eq!(rows[0], "Welcome", "the conversation stays in view");
    }

    #[test]
    fn the_inline_screen_does_not_promise_the_mouse() {
        let mut app = app();
        app.on_msg(pick_msg(&DESIGNS, None).0);
        let all = frame_rows(&mut app, 80, 10, Screen::Inline).join("\n");
        assert!(all.contains("1. Approve Design A"), "{all}");
        assert!(
            !all.contains("click") && !all.contains("reads the report"),
            "{all}"
        );
    }

    #[test]
    fn a_list_longer_than_the_room_scrolls_to_keep_the_marked_option_in_view() {
        let mut app = app();
        let many: Vec<String> = (1..=8).map(|i| format!("Option {i}")).collect();
        let refs: Vec<&str> = many.iter().map(String::as_str).collect();
        app.on_msg(pick_msg(&refs, None).0);
        let rows = frame_rows(&mut app, 80, 10, Screen::Inline);
        let all = rows.join("\n");
        assert!(
            all.contains("Option 1") && !all.contains("Option 8"),
            "{all}"
        );
        assert!(all.contains("more"), "says there is more: {all}");
        app.on_key(crossterm::event::KeyEvent::new(
            crossterm::event::KeyCode::End,
            crossterm::event::KeyModifiers::NONE,
        ));
        let all = frame_rows(&mut app, 80, 10, Screen::Inline).join("\n");
        assert!(
            all.contains("❯ 8. Option 8") || all.contains("❯ Option 8"),
            "{all}"
        );
    }

    fn mouse(kind: MouseEventKind, column: u16, row: u16) -> MouseEvent {
        MouseEvent {
            kind,
            column,
            row,
            modifiers: crossterm::event::KeyModifiers::NONE,
        }
    }

    #[test]
    fn a_click_on_an_option_picks_it_like_its_number() {
        let mut app = app();
        let (msg, answer) = pick_msg(&DESIGNS, None);
        app.on_msg(msg);
        let rows = frame_rows(&mut app, 80, 14, Screen::Alternate);
        let row = rows
            .iter()
            .position(|r| r.contains("2. Approve Design B"))
            .unwrap();
        app.on_mouse(mouse(
            MouseEventKind::Down(MouseButton::Left),
            10,
            row as u16,
        ));
        assert_eq!(answer.recv().unwrap(), Some(1));
        assert!(app.pick.is_none());
    }

    #[test]
    fn a_click_beside_the_options_picks_nothing_and_the_wheel_moves_the_mark() {
        let mut app = app();
        let (msg, answer) = pick_msg(&DESIGNS, None);
        app.on_msg(msg);
        let rows = frame_rows(&mut app, 80, 14, Screen::Alternate);
        app.on_mouse(mouse(MouseEventKind::Down(MouseButton::Left), 10, 0));
        assert!(app.pick.is_some() && answer.try_recv().is_err());
        let row = rows
            .iter()
            .position(|r| r.contains("1. Approve Design A"))
            .unwrap() as u16;
        app.on_mouse(mouse(MouseEventKind::ScrollDown, 10, row));
        assert_eq!(app.pick.as_ref().unwrap().selected, 1);
        app.on_mouse(mouse(MouseEventKind::ScrollUp, 10, row));
        assert_eq!(app.pick.as_ref().unwrap().selected, 0);
    }

    #[test]
    fn the_wheel_scrolls_the_conversation_elsewhere_and_a_click_completes_a_command() {
        let mut app = app();
        for i in 0..40 {
            app.notice(format!("line {i}"));
        }
        frame_rows(&mut app, 40, 10, Screen::Alternate);
        app.on_mouse(mouse(MouseEventKind::ScrollUp, 5, 2));
        assert_eq!(app.up, 3);
        app.on_mouse(mouse(MouseEventKind::ScrollDown, 5, 2));
        assert_eq!(app.up, 0);

        app.input.insert_str("/ev");
        let rows = frame_rows(&mut app, 100, 12, Screen::Alternate);
        let row = rows.iter().position(|r| r.contains("/evidence")).unwrap() as u16;
        let col = rows[row as usize].find("/evidence").unwrap() as u16;
        app.on_mouse(mouse(MouseEventKind::Down(MouseButton::Left), col + 2, row));
        assert_eq!(app.text(), "/evidence ");
    }

    #[test]
    fn the_line_question_shows_what_it_asks_on_the_border() {
        let mut app = app();
        let (reply, _answer) = std::sync::mpsc::channel();
        app.on_msg(Msg::Line {
            prompt: "Why: Approve Design B".into(),
            reply,
        });
        let all = frame_rows(&mut app, 80, 12, Screen::Alternate).join("\n");
        assert!(all.contains("Why: Approve Design B"), "{all}");
        assert!(all.contains("Enter writes the document"), "{all}");
    }

    #[test]
    fn jumping_to_the_top_and_then_receiving_lines_does_not_overflow() {
        let mut app = app();
        for i in 0..40 {
            app.notice(format!("line {i}"));
        }
        frame_rows(&mut app, 40, 10, Screen::Alternate);
        app.on_key(crossterm::event::KeyEvent::new(
            crossterm::event::KeyCode::Home,
            crossterm::event::KeyModifiers::CONTROL,
        ));
        let top = frame_rows(&mut app, 40, 10, Screen::Alternate);
        assert_eq!(top[0], "line 0");
        app.notice("one more");
        let again = frame_rows(&mut app, 40, 10, Screen::Alternate);
        assert_eq!(again[0], "line 0", "still at the top");
    }

    #[test]
    fn scrolling_up_keeps_the_view_when_new_lines_arrive() {
        let mut app = app();
        for i in 0..40 {
            app.notice(format!("line {i}"));
        }
        frame_rows(&mut app, 40, 10, Screen::Alternate);
        app.on_key(crossterm::event::KeyEvent::new(
            crossterm::event::KeyCode::PageUp,
            crossterm::event::KeyModifiers::NONE,
        ));
        let before = frame_rows(&mut app, 40, 10, Screen::Alternate);
        app.notice("a new line");
        let after = frame_rows(&mut app, 40, 10, Screen::Alternate);
        assert_eq!(before[0], after[0], "the top line stays");
        assert!(after.iter().any(|r| r.contains("more lines")), "{after:?}");
    }

    #[test]
    fn the_inline_screen_draws_only_the_live_rows() {
        let mut app = app();
        app.turn_started(Box::new(|| {}));
        app.apply(agent_text("first\n\nsecond\n\nthird"));
        let rows = frame_rows(&mut app, 50, 10, Screen::Inline);
        assert!(rows.iter().any(|r| r == "third"), "{rows:?}");
        assert!(rows.last().unwrap().contains("Esc cancels"), "{rows:?}");
    }

    #[test]
    fn finished_messages_go_above_the_inline_viewport_once() {
        let mut app = app();
        let mut terminal = Terminal::with_options(
            TestBackend::new(50, 14),
            TerminalOptions {
                viewport: Viewport::Inline(8),
            },
        )
        .unwrap();
        app.notice("Welcome");
        render(&mut terminal, &mut app, Screen::Inline).unwrap();
        render(&mut terminal, &mut app, Screen::Inline).unwrap();
        app.turn_started(Box::new(|| {}));
        app.apply(agent_text("streaming"));
        render(&mut terminal, &mut app, Screen::Inline).unwrap();
        app.on_msg(Msg::Done {
            stop: agent_client_protocol_schema::v1::StopReason::EndTurn,
            failed: false,
        });
        render(&mut terminal, &mut app, Screen::Inline).unwrap();
        let rows = screen_text(&terminal);
        let welcome = rows.iter().filter(|r| r.as_str() == "Welcome").count();
        let streamed = rows.iter().filter(|r| r.as_str() == "streaming").count();
        assert_eq!(welcome, 1, "written once: {rows:?}");
        assert_eq!(streamed, 1, "written once, when complete: {rows:?}");
        assert!(rows.iter().any(|r| r.contains("Ctrl+D quits")), "{rows:?}");
    }

    fn agent_text(text: &str) -> SessionUpdate {
        SessionUpdate::AgentMessageChunk(agent_client_protocol_schema::v1::ContentChunk::new(
            text.to_string().into(),
        ))
    }

    #[test]
    fn a_tiny_terminal_gets_a_plain_message_instead_of_a_panic() {
        let mut app = app();
        let rows = frame_rows(&mut app, 10, 3, Screen::Alternate);
        assert!(rows[0].contains("too small"), "{rows:?}");
    }
}
