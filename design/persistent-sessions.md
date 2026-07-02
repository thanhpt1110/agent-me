# Persistent sessions — nothing dies when the operator disconnects

**Goal (operator ask, 2026-07-02):** a running claude prompt must never be
interrupted by the operator's laptop sleeping, SSH dropping, or the browser
closing. The host is always online; every surface should exploit that, with no
manual heartbeat. The web UI must keep tracking live progress.

## Design: tmux for process persistence, transcripts for rendering

An earlier hand-written guide (`~/claude-tmux-persistent-GUIDE.md`) proved the
tmux flow manually. This feature productises it with one deliberate deviation:
the web does **NOT** render conversations from `tmux capture-pane` (TUI redraw
noise, colours, no structure). Rendering stays on the existing native-session
sync, which live-tails the CLI's own structured `~/.claude/projects` JSONL
transcripts. tmux's only job is keeping processes alive.

| Surface | Persistence mechanism | Live on web |
|---|---|---|
| Web chat (`/chat`) | already immortal — turns run in detached worker processes (own process group, `KillMode=process`); the dashboard tails the transcript on reconnect | native (SSE) |
| Web terminal (`>_` / Ctrl+~) | **new:** the PTY child is a tmux *client* (`tmux new-session -A -s agme-web`); closing the browser kills only the client, the session survives; reconnect reattaches the same screen | terminal reattach; any claude run inside also appears in the chat list via native sync |
| SSH terminal | **new:** `scripts/claude-persistent.sh [name] [claude args…]` wraps claude in `tmux new-session -A -s agme-<name>` | native-session live tail (read-only while live; adoptable after) |
| Slack bridge / brief / Auto SFA | already headless under systemd | n/a |

Notes:

- The web terminal and SSH reach the **same** tmux sessions — attach
  `agme-web-dev` from SSH, or `tmux attach -t agme-<name>` from the web
  terminal: one screen, two windows into it, full control from either side.
- Config: `CHAT_TERMINAL_PERSIST` (default on) + `CHAT_TERMINAL_TMUX_SESSION`
  (default **`agme-web-<DEPLOY_ENV or prod>`** — per-instance on purpose: DEV
  and PROD share a uid/tmux socket, so one fixed name would fuse their
  terminals into a single live screen reachable through whichever gate is
  staler); degrades to a plain non-persistent shell when tmux is missing.
  `TerminalSession.build_argv()` is the single decision point.
- **Units must use `KillMode=process`** (both dashboard units do): the tmux
  server is born inside the service's cgroup, and the default control-group
  kill would take the "persistent" session down with every release restart —
  confirmed empirically during review.
- Persistence trades away two ephemerality properties (fine for a
  single-operator host, but know them): reattach shows the session's
  **accumulated scrollback** (a fresh shell per connect used to show nothing),
  and rotating the operator passcode does not end an already-running session —
  end one explicitly with `tmux kill-session`. Rotate the dev and prod
  passcodes together.
- Closing a WebSocket sends SIGHUP/SIGTERM to the tmux client — for tmux that
  is just a detach. Ending a session for real = exit the shell inside it (or
  `tmux kill-session -t <name>`).
- Prompt injection into a running session from outside (script/cron):
  `tmux send-keys -t agme-<name> -l '<prompt>'` then a separate
  `tmux send-keys -t agme-<name> Enter`.
- For unattended long runs inside tmux, pick a permission mode up front
  (`--permission-mode acceptEdits`, an allowlist, or
  `--dangerously-skip-permissions`) so claude never blocks on an approval
  prompt overnight; `/loop` gives self-scheduled recurring work.

## Live terminals sidebar (web → any tmux session)

The chat sidebar lists **live tmux sessions** (`GET /api/chat/tmux` →
name/created/attached/windows/activity, newest activity first). Clicking one
opens the terminal drawer attached to THAT session
(`/ws/chat/terminal?session=<name>`, name validated `^[A-Za-z0-9_.-]{1,64}$`);
the drawer header shows `tmux:<name>`. This is the way back in after a closed
tab — and the way to watch/drive an agent someone started over SSH.

Research-informed choices (ttyd/GoTTY/xterm.js findings, 2026-07-02):
`new-session -A` as the idempotent attach-or-create (ttyd's documented tmux
pattern); `term.reset()` before every reattach and let tmux repaint the screen
(no server-side replay needed); chained `set-option -g window-size latest` so
the most recent client wins the size (the `smallest` default would let a small
phone viewer squeeze the SSH user). Future options if ever needed: read-only
watchers (`attach -r` = `read-only,ignore-size` — UX, not security),
ACK-based flow control for very chatty output.

## The operating model for long, self-waking agent flows

Web chat turns are `claude -p` (headless): the process **exits when the turn
ends**, so an agent that wants to poll-for-hours or self-schedule wake-ups
(`/loop`, ScheduleWakeup) cannot live there — this is why long flows "died and
never woke up". The supported pattern now:

1. Start an **interactive** claude in tmux — web terminal (`>_`, session
   `agme-web-<env>`) or SSH (`scripts/claude-persistent.sh <name>`).
2. Give it the long task, e.g. `/loop 10m check the deploy; investigate if red`
   — interactive claude self-schedules wake-ups; tmux keeps it alive with no
   client attached.
3. Watch it from anywhere: the conversation streams **live in /chat** (native
   session tail — structured, readable), and the raw terminal is one click
   away in the Live-terminals sidebar. Both survive laptop-off.

## Verification

- `test_terminal_build_argv_tmux_and_fallback` — argv contract incl. the
  no-tmux fallback.
- `test_terminal_tmux_session_survives_client_close` — real tmux: client A
  prints a marker, dies; client B reattaches and sees the same screen.
- Test suite runs with `CHAT_TERMINAL_PERSIST=0` (conftest) so WS tests never
  attach the operator's real `agme-web` session.
- Live end-to-end (guide §6 checklist): claude inside a detached tmux session,
  prompt injected via send-keys, answer visible in capture-pane, session listed
  live on `/chat`, survives with no attached client.
