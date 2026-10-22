//! The terminal: raw mode, the alternate screen or an inline viewport, and giving the terminal
//! to a step that reads the keyboard itself.

use std::io::{self, Stdout, Write};
use std::sync::atomic::{AtomicBool, Ordering};

use crossterm::cursor::Show;
use crossterm::event::{
    DisableBracketedPaste, DisableMouseCapture, EnableBracketedPaste, EnableMouseCapture,
};
use crossterm::execute;
use crossterm::terminal::{
    EnterAlternateScreen, LeaveAlternateScreen, disable_raw_mode, enable_raw_mode,
};
use ratatui::backend::CrosstermBackend;
use ratatui::{Terminal, TerminalOptions, Viewport};

/// The rows an inline viewport takes: a few lines of the message being written, the hint line, the
/// input (up to five rows with its border) and the status line.
pub const INLINE_ROWS: u16 = 10;

/// Whether a panic has to give the terminal back before it prints.
static ACTIVE: AtomicBool = AtomicBool::new(false);

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Screen {
    /// The whole terminal, like a pager: the alternate screen.
    Alternate,
    /// A few rows at the bottom of the normal screen; the conversation goes into the terminal's
    /// own scrollback, so its search, selection and copy work.
    Inline,
}

pub struct Tui {
    pub terminal: Terminal<CrosstermBackend<Stdout>>,
    pub screen: Screen,
    active: bool,
}

impl Tui {
    pub fn enter(screen: Screen) -> io::Result<Tui> {
        install_panic_hook(screen);
        let terminal = setup(screen)?;
        Ok(Tui {
            terminal,
            screen,
            active: true,
        })
    }

    /// Give the terminal back as a shell has it, to run something that reads the keyboard.
    pub fn suspend(&mut self) -> io::Result<()> {
        if !self.active {
            return Ok(());
        }
        if self.screen == Screen::Inline {
            self.terminal.clear()?;
        }
        restore(self.screen);
        self.active = false;
        Ok(())
    }

    /// Take the terminal again, and draw everything anew.
    pub fn resume(&mut self) -> io::Result<()> {
        if self.active {
            return Ok(());
        }
        self.terminal = setup(self.screen)?;
        self.active = true;
        self.terminal.clear()
    }

    /// Redraw every cell.
    pub fn clear(&mut self) -> io::Result<()> {
        self.terminal.clear()
    }
}

impl Drop for Tui {
    fn drop(&mut self) {
        if self.active {
            if self.screen == Screen::Inline {
                let _ = self.terminal.clear();
            }
            restore(self.screen);
        }
    }
}

fn setup(screen: Screen) -> io::Result<Terminal<CrosstermBackend<Stdout>>> {
    enable_raw_mode()?;
    ACTIVE.store(true, Ordering::SeqCst);
    let mut out = io::stdout();
    let entered = (|| {
        execute!(out, EnableBracketedPaste)?;
        if screen == Screen::Alternate {
            // The mouse is the full screen's: wheel and clicks work with the keys. Plain drag
            // select is then the terminal's modifier-drag (usually Shift). The inline screen
            // exists for the terminal's own selection and scrollback, so it never takes the mouse.
            execute!(out, EnterAlternateScreen, EnableMouseCapture)?;
        }
        let viewport = match screen {
            Screen::Alternate => Viewport::Fullscreen,
            Screen::Inline => Viewport::Inline(INLINE_ROWS),
        };
        Terminal::with_options(CrosstermBackend::new(out), TerminalOptions { viewport })
    })();
    if entered.is_err() {
        restore(screen);
    }
    entered
}

/// Undo `setup`; best effort, because it also runs while panicking.
fn restore(screen: Screen) {
    ACTIVE.store(false, Ordering::SeqCst);
    let mut out = io::stdout();
    let _ = execute!(out, DisableBracketedPaste, Show);
    if screen == Screen::Alternate {
        let _ = execute!(out, DisableMouseCapture, LeaveAlternateScreen);
    }
    let _ = disable_raw_mode();
    let _ = out.flush();
}

/// A panic gives the terminal back first, so its message is readable.
fn install_panic_hook(screen: Screen) {
    let previous = std::panic::take_hook();
    std::panic::set_hook(Box::new(move |info| {
        if ACTIVE.load(Ordering::SeqCst) {
            restore(screen);
        }
        previous(info);
    }));
}
