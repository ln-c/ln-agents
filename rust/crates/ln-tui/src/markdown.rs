//! Markdown as styled, already-wrapped terminal lines.
//!
//! The replies of `ln-acp`'s `render` are Markdown: headings, lists, fenced code and diffs, and
//! tables up to eight columns wide. A line here never exceeds the width it was given, so a front
//! end can draw it without wrapping again, or print it into the terminal's scrollback.

use pulldown_cmark::{CodeBlockKind, Event, HeadingLevel, Options, Parser, Tag, TagEnd};
use ratatui::style::{Color, Modifier, Style};
use ratatui::text::{Line, Span};
use unicode_width::{UnicodeWidthChar, UnicodeWidthStr};

/// How the styles look: colors, or only bold, dim and underline (`NO_COLOR`).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub struct Theme {
    color: bool,
}

impl Theme {
    pub fn new(color: bool) -> Self {
        Theme { color }
    }

    /// Colors unless `NO_COLOR` is set to anything non-empty (https://no-color.org).
    pub fn from_env() -> Self {
        Theme::new(std::env::var_os("NO_COLOR").is_none_or(|v| v.is_empty()))
    }

    pub fn color(&self) -> bool {
        self.color
    }

    fn fg(&self, color: Color) -> Style {
        if self.color {
            Style::new().fg(color)
        } else {
            Style::new()
        }
    }

    pub fn dim(&self) -> Style {
        Style::new().add_modifier(Modifier::DIM)
    }

    fn heading(&self, level: HeadingLevel) -> Style {
        let style = self.fg(Color::Cyan).add_modifier(Modifier::BOLD);
        match level {
            HeadingLevel::H1 => style.add_modifier(Modifier::UNDERLINED),
            _ => style,
        }
    }

    fn code(&self) -> Style {
        if self.color {
            Style::new().fg(Color::Yellow)
        } else {
            Style::new().add_modifier(Modifier::REVERSED)
        }
    }

    fn link(&self) -> Style {
        self.fg(Color::Blue).add_modifier(Modifier::UNDERLINED)
    }

    fn added(&self) -> Style {
        self.fg(Color::Green)
    }

    fn removed(&self) -> Style {
        self.fg(Color::Red)
    }

    fn hunk(&self) -> Style {
        self.fg(Color::Cyan)
    }
}

/// `markdown` as lines no wider than `width` columns.
pub fn render(markdown: &str, width: u16, theme: Theme) -> Vec<Line<'static>> {
    let mut writer = Writer::new(usize::from(width).max(1), theme);
    let options = Options::ENABLE_TABLES | Options::ENABLE_STRIKETHROUGH;
    for event in Parser::new_ext(markdown, options) {
        writer.event(event);
    }
    writer.finish()
}

/// Plain text wrapped to `width`, one style, for what a person typed. It breaks at the last space
/// that fits and keeps every other character, so pasted code keeps its indentation.
pub fn wrap_plain(text: &str, width: u16, style: Style) -> Vec<Line<'static>> {
    let width = usize::from(width).max(1);
    let mut lines = Vec::new();
    for raw in text.split('\n') {
        let mut parts = wrap_line(raw, width);
        if parts.is_empty() {
            parts.push(String::new());
        }
        lines.extend(
            parts
                .into_iter()
                .map(|p| Line::from(Span::styled(p, style))),
        );
    }
    lines
}

fn wrap_line(text: &str, width: usize) -> Vec<String> {
    let mut out = Vec::new();
    let mut line = String::new();
    let mut used = 0;
    // Where the line can break: the byte just after the last space, and the columns up to it.
    let mut space: Option<(usize, usize)> = None;
    for ch in text.chars() {
        let w = UnicodeWidthChar::width(ch).unwrap_or(0);
        if used + w > width && used > 0 {
            match space.take() {
                Some((at, upto)) if at < line.len() => {
                    let rest = line.split_off(at);
                    out.push(std::mem::replace(&mut line, rest));
                    used -= upto;
                }
                _ => {
                    out.push(std::mem::take(&mut line));
                    used = 0;
                }
            }
        }
        line.push(ch);
        used += w;
        if ch == ' ' {
            space = Some((line.len(), used));
        }
    }
    if !line.is_empty() {
        out.push(line);
    }
    out
}

/// The display width of a string in terminal columns.
pub fn columns(text: &str) -> usize {
    UnicodeWidthStr::width(text)
}

/// `text` cut into pieces of at most `width` columns, breaking only inside a word when one word
/// is wider than a line.
fn chunks(text: &str, width: usize) -> Vec<String> {
    let mut out = Vec::new();
    let mut line = String::new();
    let mut used = 0;
    for ch in text.chars() {
        let w = UnicodeWidthChar::width(ch).unwrap_or(0);
        if used + w > width && used > 0 {
            out.push(std::mem::take(&mut line));
            used = 0;
        }
        line.push(ch);
        used += w;
    }
    if !line.is_empty() {
        out.push(line);
    }
    out
}

struct ListLevel {
    /// The number the next item gets, or `None` for a bullet list.
    next: Option<u64>,
    /// The width of this level's marker, which the item's other lines are indented by.
    indent: usize,
}

/// One cell of a table: the styled pieces of text it holds.
type Cell = Vec<(String, Style)>;

#[derive(Default)]
struct Table {
    header: Vec<Cell>,
    rows: Vec<Vec<Cell>>,
    row: Vec<Cell>,
    cell: Cell,
    in_head: bool,
}

struct Writer {
    width: usize,
    theme: Theme,
    lines: Vec<Line<'static>>,
    /// The line being built, prefix included.
    cur: Vec<Span<'static>>,
    cur_width: usize,
    /// Whether anything but the prefix is on the current line.
    started: bool,
    /// A space is owed before the next word.
    space: bool,
    styles: Vec<Style>,
    quote: usize,
    lists: Vec<ListLevel>,
    marker: Option<String>,
    code: Option<String>,
    link: Option<String>,
    table: Option<Table>,
}

impl Writer {
    fn new(width: usize, theme: Theme) -> Self {
        Writer {
            width,
            theme,
            lines: Vec::new(),
            cur: Vec::new(),
            cur_width: 0,
            started: false,
            space: false,
            styles: vec![Style::new()],
            quote: 0,
            lists: Vec::new(),
            marker: None,
            code: None,
            link: None,
            table: None,
        }
    }

    fn style(&self) -> Style {
        self.styles.last().copied().unwrap_or_default()
    }

    fn push_style(&mut self, f: impl FnOnce(Style) -> Style) {
        let next = f(self.style());
        self.styles.push(next);
    }

    fn pop_style(&mut self) {
        if self.styles.len() > 1 {
            self.styles.pop();
        }
    }

    fn finish(mut self) -> Vec<Line<'static>> {
        self.newline();
        while self.lines.last().is_some_and(|l| l.spans.is_empty()) {
            self.lines.pop();
        }
        self.lines
    }

    /// End the current line, if it has anything on it.
    fn newline(&mut self) {
        if self.started {
            self.lines.push(Line::from(std::mem::take(&mut self.cur)));
        } else {
            self.cur.clear();
        }
        self.cur_width = 0;
        self.started = false;
        self.space = false;
    }

    /// One blank line between blocks, never two and never first.
    fn gap(&mut self) {
        self.newline();
        if self.lines.last().is_some_and(|l| !l.spans.is_empty()) {
            self.lines.push(Line::default());
        }
    }

    fn prefix(&mut self) -> Vec<Span<'static>> {
        let mut prefix = Vec::new();
        for _ in 0..self.quote {
            prefix.push(Span::styled("│ ", self.theme.dim()));
        }
        let depth = self.lists.len();
        for (i, level) in self.lists.iter().enumerate() {
            if i + 1 == depth
                && let Some(marker) = self.marker.take()
            {
                prefix.push(Span::styled(marker, self.theme.dim()));
            } else {
                prefix.push(Span::raw(" ".repeat(level.indent)));
            }
        }
        prefix
    }

    fn begin_line(&mut self) {
        if self.cur.is_empty() {
            let prefix = self.prefix();
            self.cur_width = prefix.iter().map(|s| columns(&s.content)).sum();
            self.cur = prefix;
        }
    }

    /// The columns left on a line after its prefix.
    fn room(&mut self) -> usize {
        self.begin_line();
        self.width.saturating_sub(self.cur_width).max(1)
    }

    fn put(&mut self, text: String, style: Style) {
        let w = columns(&text);
        self.cur.push(Span::styled(text, style));
        self.cur_width += w;
        self.started = true;
    }

    /// Place text word by word, wrapping at the width.
    fn text(&mut self, text: &str, style: Style) {
        if self.table.is_some() {
            if let Some(table) = self.table.as_mut() {
                table.cell.push((text.to_string(), style));
            }
            return;
        }
        let lead = text.starts_with(char::is_whitespace);
        let trail = text.ends_with(char::is_whitespace);
        if lead && self.started {
            self.space = true;
        }
        for word in text.split_whitespace() {
            self.word(word, style);
            self.space = true;
        }
        if !trail && !text.trim().is_empty() {
            self.space = false;
        }
    }

    fn word(&mut self, word: &str, style: Style) {
        self.begin_line();
        let w = columns(word);
        let owed = usize::from(self.space && self.started);
        if self.started && self.cur_width + owed + w > self.width {
            self.newline();
            self.begin_line();
        } else if owed == 1 {
            self.put(" ".to_string(), self.style());
        }
        self.space = false;
        let room = self.width.saturating_sub(self.cur_width).max(1);
        if w <= room {
            self.put(word.to_string(), style);
            return;
        }
        for piece in chunks(word, room) {
            if self.started && self.cur_width + columns(&piece) > self.width {
                self.newline();
                self.begin_line();
            }
            self.put(piece, style);
        }
    }

    fn code_line(&mut self, line: &str, style: Style) {
        self.begin_line();
        self.put("  ".to_string(), Style::new());
        let room = self.room();
        let pieces = chunks(line, room);
        if pieces.is_empty() {
            self.newline_force();
            return;
        }
        for (i, piece) in pieces.into_iter().enumerate() {
            if i > 0 {
                self.newline_force();
                self.begin_line();
                self.put("  ".to_string(), Style::new());
            }
            self.put(piece, style);
        }
        self.newline_force();
    }

    /// End the line even when it is empty (a blank line of code).
    fn newline_force(&mut self) {
        self.started = true;
        self.newline();
    }

    fn code_style(&self, lang: &str, line: &str) -> Style {
        if lang == "diff" || lang == "patch" {
            match line.chars().next() {
                Some('+') => return self.theme.added(),
                Some('-') => return self.theme.removed(),
                Some('@') => return self.theme.hunk(),
                _ => {}
            }
        }
        self.theme.code()
    }

    fn event(&mut self, event: Event<'_>) {
        match event {
            Event::Start(tag) => self.start(tag),
            Event::End(tag) => self.end(tag),
            Event::Text(text) => {
                if let Some(lang) = self.code.clone() {
                    for line in text.split_inclusive('\n') {
                        let line = line.trim_end_matches('\n');
                        let style = self.code_style(&lang, line);
                        self.code_line(line, style);
                    }
                } else {
                    let style = self.style();
                    self.text(&text, style);
                }
            }
            Event::Code(text) => {
                let style = self.theme.code();
                self.inline_word(&text, style);
            }
            Event::Html(html) | Event::InlineHtml(html) => {
                let style = self.style();
                self.text(&html, style);
            }
            Event::SoftBreak => self.space = self.started || self.table.is_some(),
            Event::HardBreak => {
                if self.table.is_some() {
                    self.space = true;
                } else {
                    self.newline();
                }
            }
            Event::Rule => {
                self.gap();
                let rule = "─".repeat(self.width.min(48));
                self.put(rule, self.theme.dim());
                self.gap();
            }
            Event::TaskListMarker(done) => {
                let mark = if done { "[x] " } else { "[ ] " };
                self.put(mark.to_string(), Style::new());
            }
            Event::FootnoteReference(name) => {
                let style = self.theme.dim();
                self.text(&format!("[{name}]"), style);
            }
            _ => {}
        }
    }

    /// Inline code: the words keep the code style, and may wrap at spaces like any word.
    fn inline_word(&mut self, text: &str, style: Style) {
        if self.table.is_some() {
            if let Some(table) = self.table.as_mut() {
                table.cell.push((text.to_string(), style));
            }
            return;
        }
        if text.starts_with(char::is_whitespace) && self.started {
            self.space = true;
        }
        for word in text.split_whitespace() {
            self.word(word, style);
            self.space = true;
        }
        self.space = text.ends_with(char::is_whitespace);
    }

    fn start(&mut self, tag: Tag<'_>) {
        match tag {
            Tag::Paragraph => {
                if self.lists.is_empty() {
                    self.gap();
                } else {
                    self.newline();
                }
            }
            Tag::Heading { level, .. } => {
                self.gap();
                let style = self.theme.heading(level);
                self.push_style(|_| style);
            }
            Tag::BlockQuote(_) => {
                self.gap();
                self.quote += 1;
            }
            Tag::CodeBlock(kind) => {
                self.gap();
                let lang = match kind {
                    CodeBlockKind::Fenced(lang) => {
                        lang.split_whitespace().next().unwrap_or("").to_string()
                    }
                    CodeBlockKind::Indented => String::new(),
                };
                self.code = Some(lang);
            }
            Tag::List(first) => {
                if self.lists.is_empty() {
                    self.gap();
                } else {
                    self.newline();
                }
                self.lists.push(ListLevel {
                    next: first,
                    indent: 2,
                });
            }
            Tag::Item => {
                self.newline();
                let marker = match self.lists.last_mut() {
                    Some(level) => match level.next {
                        Some(n) => {
                            level.next = Some(n + 1);
                            format!("{n}. ")
                        }
                        None => "• ".to_string(),
                    },
                    None => "• ".to_string(),
                };
                if let Some(level) = self.lists.last_mut() {
                    level.indent = columns(&marker);
                }
                self.marker = Some(marker);
            }
            Tag::Emphasis => self.push_style(|s| s.add_modifier(Modifier::ITALIC)),
            Tag::Strong => self.push_style(|s| s.add_modifier(Modifier::BOLD)),
            Tag::Strikethrough => self.push_style(|s| s.add_modifier(Modifier::CROSSED_OUT)),
            Tag::Link { dest_url, .. } => {
                self.link = Some(dest_url.to_string());
                let link = self.theme.link();
                self.push_style(|_| link);
            }
            Tag::Image { dest_url, .. } => {
                self.link = Some(dest_url.to_string());
            }
            Tag::Table(_) => {
                self.gap();
                self.table = Some(Table::default());
            }
            Tag::TableHead => {
                if let Some(table) = self.table.as_mut() {
                    table.in_head = true;
                }
            }
            Tag::TableRow => {}
            Tag::TableCell => {
                if let Some(table) = self.table.as_mut() {
                    table.cell.clear();
                }
            }
            _ => {}
        }
    }

    fn end(&mut self, tag: TagEnd) {
        match tag {
            TagEnd::Paragraph => {
                if self.lists.is_empty() {
                    self.gap();
                } else {
                    self.newline();
                }
            }
            TagEnd::Heading(_) => {
                self.pop_style();
                self.gap();
            }
            TagEnd::BlockQuote(_) => {
                self.quote = self.quote.saturating_sub(1);
                self.gap();
            }
            TagEnd::CodeBlock => {
                self.code = None;
                self.gap();
            }
            TagEnd::List(_) => {
                self.lists.pop();
                if self.lists.is_empty() {
                    self.gap();
                }
            }
            TagEnd::Item => self.newline(),
            TagEnd::Emphasis | TagEnd::Strong | TagEnd::Strikethrough => self.pop_style(),
            TagEnd::Link | TagEnd::Image => {
                if matches!(tag, TagEnd::Link) {
                    self.pop_style();
                }
                if let Some(url) = self.link.take() {
                    let shown = self.shown_text_is(&url);
                    if !shown && !url.is_empty() {
                        let style = self.theme.dim();
                        self.space = true;
                        self.text(&format!("({url})"), style);
                    }
                }
            }
            TagEnd::TableHead => {
                if let Some(table) = self.table.as_mut() {
                    table.header = std::mem::take(&mut table.row);
                    table.in_head = false;
                }
            }
            TagEnd::TableRow => {
                if let Some(table) = self.table.as_mut() {
                    let row = std::mem::take(&mut table.row);
                    table.rows.push(row);
                }
            }
            TagEnd::TableCell => {
                if let Some(table) = self.table.as_mut() {
                    let cell = std::mem::take(&mut table.cell);
                    table.row.push(cell);
                }
            }
            TagEnd::Table => {
                if let Some(table) = self.table.take() {
                    self.table(table);
                }
                self.gap();
            }
            _ => {}
        }
    }

    /// Whether the text just written is the URL itself (an autolink), so it is not repeated.
    fn shown_text_is(&self, url: &str) -> bool {
        let last: String = self
            .cur
            .iter()
            .rev()
            .take(3)
            .map(|s| s.content.to_string())
            .collect::<Vec<_>>()
            .into_iter()
            .rev()
            .collect();
        last.trim_end().ends_with(url)
    }

    fn table(&mut self, table: Table) {
        let columns_n = table
            .rows
            .iter()
            .chain(std::iter::once(&table.header))
            .map(Vec::len)
            .max()
            .unwrap_or(0);
        if columns_n == 0 {
            return;
        }
        self.begin_line();
        let avail = self.width.saturating_sub(self.cur_width).max(1);
        let plain = |cell: &Cell| cell.iter().map(|(t, _)| columns(t)).sum::<usize>();
        let longest_word = |cell: &Cell| {
            let text: String = cell.iter().map(|(t, _)| t.as_str()).collect();
            text.split_whitespace().map(columns).max().unwrap_or(0)
        };
        // A column is as wide as its widest cell, and may shrink to its longest word: a word is
        // never cut in a table. When even that does not fit, the rows are stacked instead.
        let mut widths = vec![1usize; columns_n];
        let mut floors = vec![1usize; columns_n];
        for row in std::iter::once(&table.header).chain(table.rows.iter()) {
            for (i, cell) in row.iter().enumerate() {
                widths[i] = widths[i].max(plain(cell));
                floors[i] = floors[i].max(longest_word(cell));
            }
        }
        let frame = 3 * columns_n + 1;
        if floors.iter().sum::<usize>() + frame > avail {
            self.stacked(&table, avail);
            return;
        }
        while widths.iter().sum::<usize>() + frame > avail {
            let shrinkable = widths
                .iter()
                .enumerate()
                .filter(|(i, w)| **w > floors[*i])
                .max_by_key(|(_, w)| **w)
                .map(|(i, _)| i);
            match shrinkable {
                Some(i) => widths[i] -= 1,
                None => break,
            }
        }
        let border = self.theme.dim();
        let rule = |left: &str, mid: &str, right: &str| {
            let cells: Vec<String> = widths.iter().map(|w| "─".repeat(w + 2)).collect();
            Line::from(Span::styled(
                format!("{left}{}{right}", cells.join(mid)),
                border,
            ))
        };
        let mut out = vec![rule("┌", "┬", "┐")];
        let bold = |cell: &Cell| -> Cell {
            cell.iter()
                .map(|(t, s)| (t.clone(), s.add_modifier(Modifier::BOLD)))
                .collect()
        };
        let header: Vec<Cell> = table.header.iter().map(bold).collect();
        out.extend(table_row(&header, &widths, border));
        out.push(rule("├", "┼", "┤"));
        for row in &table.rows {
            out.extend(table_row(row, &widths, border));
        }
        out.push(rule("└", "┴", "┘"));
        for line in out {
            self.begin_line();
            let mut spans = std::mem::take(&mut self.cur);
            spans.extend(line.spans);
            self.lines.push(Line::from(spans));
            self.cur_width = 0;
            self.started = false;
        }
    }

    /// A table too wide for the line: each row is its first cell in bold, then one `header: value`
    /// line for every other cell, wrapped under the label, with a blank line between rows.
    fn stacked(&mut self, table: &Table, avail: usize) {
        let text_of = |cell: &Cell| cell.iter().map(|(t, _)| t.as_str()).collect::<String>();
        for (n, row) in table.rows.iter().enumerate() {
            if n > 0 {
                self.newline();
                self.lines.push(Line::default());
            }
            for (i, cell) in row.iter().enumerate() {
                let value = text_of(cell);
                if i == 0 {
                    self.stacked_line(&value, Style::new().add_modifier(Modifier::BOLD), avail);
                    continue;
                }
                let label = table.header.get(i).map(text_of).unwrap_or_default();
                let lead = format!("  {label}: ");
                let room = avail.saturating_sub(columns(&lead)).max(1);
                let longest = value.split_whitespace().map(columns).max().unwrap_or(0);
                if longest > room {
                    // a word that does not fit after the label goes on lines of its own
                    self.stacked_line(&format!("  {label}:"), Style::new(), avail);
                    let room = avail.saturating_sub(4).max(1);
                    for piece in wrap_line(&value, room) {
                        self.stacked_line(&format!("    {piece}"), Style::new(), avail);
                    }
                    continue;
                }
                for (k, piece) in wrap_line(&value, room).into_iter().enumerate() {
                    let text = if k == 0 {
                        format!("{lead}{piece}")
                    } else {
                        format!("{}{piece}", " ".repeat(columns(&lead)))
                    };
                    self.stacked_line(&text, Style::new(), avail);
                }
            }
        }
    }

    fn stacked_line(&mut self, text: &str, style: Style, avail: usize) {
        self.newline();
        self.begin_line();
        let mut first = true;
        for piece in chunks(text.trim_end(), avail) {
            if !first {
                self.newline_force();
                self.begin_line();
            }
            first = false;
            self.put(piece, style);
        }
        self.newline_force();
    }
}

/// The lines of one table row: every cell wrapped to its column, side by side.
fn table_row(cells: &[Cell], widths: &[usize], border: Style) -> Vec<Line<'static>> {
    let wrapped: Vec<Vec<Vec<(String, Style)>>> = widths
        .iter()
        .enumerate()
        .map(|(i, w)| wrap_cell(cells.get(i).map(Vec::as_slice).unwrap_or(&[]), *w))
        .collect();
    let height = wrapped.iter().map(Vec::len).max().unwrap_or(1).max(1);
    (0..height)
        .map(|row| {
            let mut spans = vec![Span::styled("│", border)];
            for (i, w) in widths.iter().enumerate() {
                let pieces = wrapped[i].get(row).cloned().unwrap_or_default();
                let used: usize = pieces.iter().map(|(t, _)| columns(t)).sum();
                spans.push(Span::raw(" "));
                for (text, style) in pieces {
                    spans.push(Span::styled(text, style));
                }
                spans.push(Span::raw(" ".repeat(w.saturating_sub(used) + 1)));
                spans.push(Span::styled("│", border));
            }
            Line::from(spans)
        })
        .collect()
}

/// A cell's text broken into lines of at most `width` columns, at spaces where it can.
fn wrap_cell(cell: &[(String, Style)], width: usize) -> Vec<Vec<(String, Style)>> {
    let mut lines: Vec<Vec<(String, Style)>> = vec![Vec::new()];
    let mut used = 0usize;
    for (text, style) in cell {
        for (i, word) in text.split(' ').enumerate() {
            if word.is_empty() {
                continue;
            }
            let w = columns(word);
            let owed = usize::from(used > 0 && (i > 0 || text.starts_with(' ')));
            if used > 0 && used + owed + w > width {
                lines.push(Vec::new());
                used = 0;
            }
            let space = if used > 0 && owed == 1 { " " } else { "" };
            if w <= width.saturating_sub(used + space.len()) {
                let piece = format!("{space}{word}");
                used += columns(&piece);
                if let Some(line) = lines.last_mut() {
                    line.push((piece, *style));
                }
                continue;
            }
            for piece in chunks(word, width) {
                if used > 0 {
                    lines.push(Vec::new());
                }
                used = columns(&piece);
                if let Some(line) = lines.last_mut() {
                    line.push((piece, *style));
                }
            }
        }
    }
    lines
}

#[cfg(test)]
mod tests {
    use super::*;

    fn plain(markdown: &str, width: u16) -> Vec<String> {
        render(markdown, width, Theme::new(false))
            .iter()
            .map(|l| l.spans.iter().map(|s| s.content.as_ref()).collect())
            .collect()
    }

    #[test]
    fn a_heading_and_a_paragraph_are_separated_by_one_blank_line() {
        assert_eq!(
            plain("# Title\n\nSome text.\n", 40),
            vec!["Title", "", "Some text."]
        );
    }

    #[test]
    fn a_paragraph_wraps_at_the_width_without_splitting_words() {
        assert_eq!(
            plain("one two three four five six", 12),
            vec!["one two", "three four", "five six"]
        );
    }

    #[test]
    fn a_word_wider_than_the_line_is_cut() {
        assert_eq!(plain("abcdefghij", 4), vec!["abcd", "efgh", "ij"]);
    }

    #[test]
    fn wide_characters_count_two_columns() {
        let lines = plain("日本語のテキスト", 8);
        assert!(lines.iter().all(|l| columns(l) <= 8), "{lines:?}");
        assert_eq!(lines.concat(), "日本語のテキスト");
    }

    #[test]
    fn emphasis_and_code_keep_their_style_across_a_wrap() {
        let lines = render("a **bold** and `code` here", 40, Theme::new(true));
        let spans = &lines[0].spans;
        let bold = spans.iter().find(|s| s.content == "bold").unwrap();
        assert!(bold.style.add_modifier.contains(Modifier::BOLD));
        let code = spans.iter().find(|s| s.content == "code").unwrap();
        assert_eq!(code.style.fg, Some(Color::Yellow));
        let text: String = spans.iter().map(|s| s.content.as_ref()).collect();
        assert_eq!(text, "a bold and code here");
    }

    #[test]
    fn without_color_nothing_sets_a_foreground() {
        let lines = render("# H\n\n`c` [l](http://x)\n", 40, Theme::new(false));
        for line in &lines {
            for span in &line.spans {
                assert_eq!(span.style.fg, None, "{span:?}");
            }
        }
    }

    #[test]
    fn lists_are_marked_and_nested_lists_are_indented() {
        assert_eq!(
            plain("- one\n- two\n  - inner\n- three\n\n1. a\n2. b\n", 40),
            vec!["• one", "• two", "  • inner", "• three", "", "1. a", "2. b"]
        );
    }

    #[test]
    fn a_wrapped_list_item_is_indented_under_its_marker() {
        assert_eq!(
            plain("- alpha beta gamma delta", 14),
            vec!["• alpha beta", "  gamma delta"]
        );
    }

    #[test]
    fn a_fenced_block_is_indented_and_never_wrapped_by_words() {
        assert_eq!(
            plain("text\n\n```sh\nls -l\nmore\n```\n", 40),
            vec!["text", "", "  ls -l", "  more"]
        );
    }

    #[test]
    fn a_diff_colors_added_and_removed_lines() {
        let lines = render(
            "```diff\n+new\n-old\n@@ x\n ctx\n```\n",
            40,
            Theme::new(true),
        );
        let fg = |i: usize| lines[i].spans.last().unwrap().style.fg;
        assert_eq!(fg(0), Some(Color::Green));
        assert_eq!(fg(1), Some(Color::Red));
        assert_eq!(fg(2), Some(Color::Cyan));
        assert_eq!(fg(3), Some(Color::Yellow));
    }

    #[test]
    fn a_blockquote_gets_a_bar() {
        assert_eq!(plain("> quoted\n", 40), vec!["│ quoted"]);
    }

    #[test]
    fn a_link_shows_its_address_unless_the_text_is_the_address() {
        assert_eq!(
            plain("[docs](https://example.org/a)", 60),
            vec!["docs (https://example.org/a)"]
        );
        assert_eq!(
            plain("<https://example.org/a>", 60),
            vec!["https://example.org/a"]
        );
    }

    #[test]
    fn a_table_that_fits_is_drawn_as_a_grid() {
        let lines = plain("| a | b |\n|---|---|\n| 1 | 2 |\n", 40);
        assert_eq!(
            lines,
            vec![
                "┌───┬───┐",
                "│ a │ b │",
                "├───┼───┤",
                "│ 1 │ 2 │",
                "└───┴───┘"
            ]
        );
    }

    #[test]
    fn a_wide_table_shrinks_its_widest_columns_and_wraps_cells() {
        let md = "| name | note |\n|---|---|\n| x | a long note that does not fit on one line |\n";
        let lines = plain(md, 30);
        assert!(lines.iter().all(|l| columns(l) <= 30), "{lines:?}");
        assert!(
            lines.len() > 5,
            "the note wraps over several rows: {lines:?}"
        );
        let all = lines.join(" ");
        assert!(all.contains("long") && all.contains("fit"), "{all}");
    }

    #[test]
    fn a_table_too_narrow_for_its_columns_is_stacked() {
        let md =
            "| model | provider | family | where |\n|---|---|---|---|\n| m1 | p1 | f1 | w1 |\n";
        let lines = plain(md, 20);
        assert!(lines.iter().all(|l| columns(l) <= 20), "{lines:?}");
        assert_eq!(lines[0], "m1");
        assert!(lines.contains(&"  provider: p1".to_string()), "{lines:?}");
    }

    #[test]
    fn a_table_never_cuts_a_word_it_stacks_the_rows_instead() {
        let md = "| model | provider |\n|---|---|\n| claude_opus_5_5_high_cli | claude_pro:claude-opus-5-5 |\n| gemini_3_8_flash_high | google_ai_pro:gemini-3.8-flash-high |\n";
        let lines = plain(md, 40);
        let all = lines.join("\n");
        for word in [
            "claude_opus_5_5_high_cli",
            "claude_pro:claude-opus-5-5",
            "google_ai_pro:gemini-3.8-flash-high",
        ] {
            assert!(all.contains(word), "{word} is whole: {all}");
        }
        assert!(lines.iter().all(|l| columns(l) <= 40), "{lines:?}");
        assert!(
            lines.contains(&String::new()),
            "rows are told apart: {lines:?}"
        );
        // wide enough, it is a grid again
        let wide = plain(md, 100);
        assert!(wide[0].starts_with('┌'), "{wide:?}");
    }

    #[test]
    fn no_line_is_ever_wider_than_the_width() {
        let md = "# T\n\n- item `with_a_very_long_code_span_that_will_not_fit` end\n\n| a | b |\n|---|---|\n| aaaaaaaaaaaaaaaa | bbbbbbbbbbbbbbbbbb |\n\n> q q q q q q q q q q q q q q\n";
        for width in [8u16, 12, 20, 33, 80] {
            let lines = plain(md, width);
            assert!(
                lines.iter().all(|l| columns(l) <= usize::from(width)),
                "width {width}: {lines:?}"
            );
        }
    }

    #[test]
    fn plain_text_keeps_its_line_breaks_and_wraps() {
        let lines = wrap_plain("ab cd\n\nef", 3, Style::new());
        let text: Vec<String> = lines
            .iter()
            .map(|l| l.spans.iter().map(|s| s.content.as_ref()).collect())
            .collect();
        assert_eq!(text, vec!["ab ", "cd", "", "ef"]);
    }
}
