# Terminal UI

`ln-agents` with no subcommand opens a terminal UI over the same [slash commands](editor-commands.md) as the editor integration, and `ln-agents -p "<prompt>"` runs one prompt and prints the answer. Both call the A2A server (started with `serve a2a`, see [Command line](cli.md)), which must be running; `ln-agents` does not start it.

```bash
cd rust && cargo build --release      # target/release/ln-agents
ln-agents                                  # the terminal UI, in the project's directory
ln-agents --mode design                    # may also start runs, write unsigned documents and sign
ln-agents --no-alt-screen                  # the conversation goes into the terminal's own scrollback
ln-agents -p "/models"                     # one prompt; the answer is on stdout
echo "/status my-run" | ln-agents -p       # the prompt from stdin
```

The URL, token file, signing key and runs directory are the same flags and `LN_AGENTS_*` variables as `ln-agents acp` ([Editor commands](editor-commands.md)); the token file defaults to `<current directory>/.runs/.a2a/token`. The session starts in the read-only `status` mode; `--mode design` or `/mode design` allows the commands that change something.

## What the screen shows

- **The conversation**, with each reply drawn from its Markdown: headings, lists, code and diffs, and tables. A table too wide for the terminal is shown as rows of `header: value` lines instead of cutting words.
- **The stages of the run you are driving**, above the input: `■G ■A1 ▶A3` is grounding and the first design stage done and the third working, with the run's id, how long the turn has been going, and `Esc cancels`. When the turn has ended, the last stage is drawn `□`: it is where the run stopped, and the message above says why (waiting for your signature, paused, failed). Only the stages the run has entered are shown; the ones still to come are not known to `ln-agents`. When a run goes back to an earlier stage the list shows the stages entered in the order they were entered.
- **The commands that match**, in place of the stages, while you type a `/`. `Tab` completes.
- **A status line**: the mode, the run, and the keys that matter now.

## Keys

| Key | Does |
|---|---|
| `Enter` | Send. `Alt+Enter` or `Ctrl+J` starts a new line. |
| `Tab` | Complete a command (the longest part the matches share). |
| `Up`, `Down` | At the first or last line of the input, walk what you sent in this session. |
| `Esc`, `Ctrl+C` | Cancel the running turn: the A2A task is cancelled, which stops the run after its current step. Nothing is undone. A second `Ctrl+C` leaves at once. With nothing running, `Ctrl+C` clears the line, then leaves. |
| `Ctrl+D` | Leave, from an empty line. |
| `Ctrl+L` | Redraw. |
| `PageUp`, `PageDown`, `Ctrl+Home`, `Ctrl+End` | Scroll the full-screen view. |

`/mode [status|design]`, `/clear`, `/help` and `/quit` are answered by the UI itself; everything else is the same code as in an editor.

## Full screen and inline

By default the UI uses the terminal's alternate screen, as a pager does: the input stays at the bottom and the conversation scrolls above it. The terminal's own scrollback, search and selection do not see it; use `PageUp` and `PageDown`, and select with your terminal's modifier key.

`--no-alt-screen` (or `LN_AGENTS_NO_ALT_SCREEN=1`; empty, `0`, `false`, `no` and `off` mean off) uses a few rows at the bottom of the normal screen instead. Each finished message is written into the terminal's scrollback, so its search, selection and copy work, and the text stays when you leave. A message that is still being written is shown in the live rows and moves into the scrollback when it is complete. Use it when you want the terminal's own scrollback, search and copy. It asks the terminal for the cursor position at start, so a terminal that does not answer cannot use it.

`NO_COLOR` turns colors off; bold, dim and underline remain.

## Signing in the same terminal

`/decide` and `/resume` (design mode) do not open a window here. The UI gives the terminal back, runs `ln-agents decide` or `ln-agents resume` in it, and comes back when it ends. That step is the one an editor opens in a window: it shows the run, node, decision, candidate, approver and reason, asks you to type the run id, and runs `ssh-keygen -Y sign` so it asks for your passphrase there. The UI reads no key while it runs, and neither it nor any model sees the run id or the passphrase. The step drops `SSH_AUTH_SOCK` and `SSH_ASKPASS_REQUIRE`, so a key loaded in an ssh-agent still asks for its passphrase. See [Human decisions](../guides/human-decisions.md).

`/approval-doc` asks before it writes the unsigned document: `y` allows it once, and `n`, `Esc` or `Enter` does not.

## `-p`

`-p` runs one prompt in `--mode` (default `status`) and prints the reply as Markdown on stdout. The stages and model calls of a run, and any error, go to stderr. Nothing is asked of anyone: `/approval-doc` is not written, and `/decide` and `/resume` need `ln-agents` without `-p`. The exit status is 0 for an answer, 1 when the command failed or was refused (for example a design command in `status` mode), and 2 for a usage error.

Without a terminal on both input and output, `ln-agents` with no `-p` says so and exits with status 1.
