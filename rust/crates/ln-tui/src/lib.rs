//! The terminal UI of `ln-agents`: the slash commands of the ACP agent, and questions for the
//! coordinator, in a terminal. It is a front end only. Every turn is the same code as an editor's
//! (`ln_acp::turn`), over one A2A skill of `ln-pipeline`; nothing here approves, decides or
//! resumes a run. `/decide` and `/resume` hand the terminal to a step the binary runs
//! ([`Foreground`]), where the person types the run id and their passphrase.
//!
//! This crate starts no process: the binary implements [`Foreground`].

pub mod app;
pub mod host;
pub mod markdown;
mod term;
pub mod ui;

use std::io::IsTerminal;
use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::sync::mpsc::{Receiver, Sender, channel};
use std::thread;
use std::time::Duration;

use crossterm::event::{self, Event};
use ln_acp::commands::Mode;
use ln_acp::turn::Prompt;

use app::{Action, App, Msg};
use host::{Shared, TuiHost};
pub use term::Screen;
use term::Tui;

/// Runs a step of `ln-agents` that reads the keyboard itself, attached to this terminal.
pub trait Foreground: Send + Sync {
    /// Run `ln-agents <args>` in `cwd` and wait; its exit status, or why it could not start.
    fn run(&self, cwd: &Path, args: &[String]) -> Result<i32, String>;
}

pub struct Options {
    /// The orchestrator's address and token, and the settings of `/decide` and `/resume`.
    pub config: ln_acp::Config,
    pub cwd: PathBuf,
    pub mode: Mode,
    pub screen: Screen,
    pub foreground: Arc<dyn Foreground>,
}

/// Why there is no terminal UI to show, if there is not.
pub fn needs_terminal() -> Result<(), String> {
    if std::io::stdin().is_terminal() && std::io::stdout().is_terminal() {
        Ok(())
    } else {
        Err(
            "ln-agents needs a terminal on both input and output. For a script, use \
             `ln-agents -p \"<prompt>\"`."
                .to_string(),
        )
    }
}

/// Show the terminal UI until the person leaves.
pub fn run(options: Options) -> Result<(), String> {
    needs_terminal()?;
    let Options {
        config,
        cwd,
        mode,
        screen,
        foreground,
    } = options;
    let config = Arc::new(config);
    let theme = markdown::Theme::from_env();
    let mut app = App::new(mode, theme);
    app.notice(format!(
        "ln-agents {} — the terminal front end of ln-pipeline. Orchestrator: {} (start it with \
         `python -m agentic_pipeline serve a2a`). Mode: {}. `/help` lists the commands and keys.",
        env!("CARGO_PKG_VERSION"),
        config.url,
        mode.name(),
    ));
    let (msg_tx, msg_rx) = channel();
    let shared = Arc::new(Shared::default());
    let mut tui = Tui::enter(screen).map_err(|e| format!("cannot use the terminal: {e}"))?;
    let outcome = event_loop(
        &mut tui,
        &mut app,
        &config,
        &cwd,
        &shared,
        &msg_tx,
        &msg_rx,
        foreground.as_ref(),
    );
    drop(tui);
    outcome.map_err(|e| format!("terminal: {e}"))
}

#[allow(clippy::too_many_arguments)]
fn event_loop(
    tui: &mut Tui,
    app: &mut App,
    config: &Arc<ln_acp::Config>,
    cwd: &Path,
    shared: &Arc<Shared>,
    msg_tx: &Sender<Msg>,
    msg_rx: &Receiver<Msg>,
    foreground: &dyn Foreground,
) -> std::io::Result<()> {
    loop {
        ui::render(&mut tui.terminal, app, tui.screen)?;
        if app.quit {
            return Ok(());
        }
        let wait = if app.busy() {
            Duration::from_millis(80)
        } else {
            Duration::from_millis(300)
        };
        if event::poll(wait)? {
            // Everything that has already arrived is handled before the next draw: a wheel or a
            // held key sends many events at once.
            loop {
                let action = match event::read()? {
                    Event::Key(key) => app.on_key(key),
                    Event::Paste(text) => {
                        app.on_paste(&text);
                        None
                    }
                    Event::Mouse(mouse) => {
                        app.on_mouse(mouse);
                        None
                    }
                    _ => None, // a resize is drawn on the next turn of the loop
                };
                match action {
                    Some(Action::Run { text, mode, run }) => {
                        start_turn(app, config, cwd, shared, msg_tx, text, mode, run);
                    }
                    Some(Action::Redraw) => tui.clear()?,
                    None => {}
                }
                if app.quit || !event::poll(Duration::ZERO)? {
                    break;
                }
            }
        }
        while let Ok(msg) = msg_rx.try_recv() {
            match msg {
                Msg::Foreground { cwd, args, reply } => {
                    let result = step_in_terminal(tui, foreground, &cwd, &args)?;
                    let _ = reply.send(result);
                }
                other => app.on_msg(other),
            }
        }
    }
}

#[allow(clippy::too_many_arguments)]
fn start_turn(
    app: &mut App,
    config: &Arc<ln_acp::Config>,
    cwd: &Path,
    shared: &Arc<Shared>,
    msg_tx: &Sender<Msg>,
    text: String,
    mode: Mode,
    run: Option<String>,
) {
    let host = TuiHost {
        config: Arc::clone(config),
        cwd: cwd.to_path_buf(),
        msg: msg_tx.clone(),
        shared: Arc::clone(shared),
    };
    let (prompt, cancel) = Prompt::new(host, cwd.to_path_buf(), run);
    app.turn_started(Box::new(move || cancel.cancel()));
    let done = msg_tx.clone();
    thread::spawn(move || {
        let outcome = prompt.run_outcome(&text, mode);
        let _ = done.send(Msg::Done {
            stop: outcome.stop,
            failed: outcome.failed,
        });
    });
}

/// Give the terminal to a step, wait for it, and take it back. The loop reads no key meanwhile,
/// so the run id and the passphrase go to the step; keys typed ahead are dropped afterwards.
fn step_in_terminal(
    tui: &mut Tui,
    foreground: &dyn Foreground,
    cwd: &Path,
    args: &[String],
) -> std::io::Result<Result<i32, String>> {
    tui.suspend()?;
    let result = foreground.run(cwd, args);
    while event::poll(Duration::ZERO).unwrap_or(false) {
        let _ = event::read();
    }
    tui.resume()?;
    Ok(result)
}
