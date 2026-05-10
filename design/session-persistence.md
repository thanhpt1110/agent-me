# Session persistence — Slack DM ↔ Claude Code

_Added 2026-05-10. Replaces the original "every message is a fresh
`claude -p` call" model._

## Problem

Before this change, each Slack DM message was answered by spawning a
new `claude -p <prompt>` subprocess. No `--continue`, no `--resume`,
no history fed back in. The bridge was already saving messages to a
SQLite `messages` table per `thread_ts`, but nothing read that table
back. Net effect: claude couldn't remember anything you said earlier
in the same conversation. Asking "what did I just say?" returned a
blank stare. Multi-turn chat in Slack didn't work.

We had two options:

1. **Inject history into every prompt.** Read the `messages` table,
   format it as a chat transcript, prepend to the new prompt. Works,
   but the prompt grows linearly each turn, breaks the prompt cache,
   and you pay full input tokens every time.

2. **Use Claude Code's built-in session mechanism.** Claude Code
   stores sessions on disk (under `~/.claude/projects/<sanitized cwd>/`)
   and provides `--resume <uuid>` to continue one. The CLI returns
   `session_id` in `--output-format json` output. So all the bridge has
   to do is: capture the id on first call, save it under `thread_ts`,
   and pass `--resume <id>` on the next call.

Option 2 is what we shipped. Cache hits jump from 0 → 76k tokens on
turn 2 in our smoke test (haiku-4-5, simple prompt) — i.e. claude
re-reads the system prompt + tool catalogs from cache instead of paying
full freight every turn. This is a major cost win for long
conversations.

## How it works

```
Slack message arrives
  ↓
on_message → handle_user_query(thread_ts=…, text=…)
  ↓
get_session_id(thread_ts) → "abc-123…" (or None for first turn)
  ↓
spawn_claude(text, resume_session_id="abc-123…")
  └── runs:  claude -p <text> --output-format json --resume abc-123…
  ↓
claude returns:  {"result": "…", "session_id": "abc-123…", "usage": {…}}
  ↓
upsert_session(thread_ts, "abc-123…")  ← saved for next turn
  ↓
post answer in Slack
```

Each Slack `thread_ts` maps to exactly one `session_id`. New thread
(top-level message OR new in-thread reply with no parent) ⇒ no entry
⇒ first call has `--resume` omitted ⇒ claude creates a fresh session
and returns its id ⇒ bridge stores it.

## Schema addition

```sql
CREATE TABLE claude_sessions (
    thread_ts     TEXT PRIMARY KEY,
    session_id    TEXT NOT NULL,
    started_at    INTEGER NOT NULL,
    last_used_at  INTEGER NOT NULL,
    turn_count    INTEGER NOT NULL DEFAULT 0
);
```

`thread_ts` is whatever Slack hands us:
- For a reply inside a Slack thread: the parent message's ts.
- For a top-level message in a DM: the message's own ts (Slack treats
  it as a one-message thread).

So **top-level DM messages each become their own session**. To get
multi-turn continuity, *reply inside a Slack thread* — the thread_ts
stays constant across replies, and the bridge keeps resuming the same
session. (Yes, Slack threads in DMs are a slightly clunky UX, but
they're unambiguous and they map cleanly to claude's session model. If
we ever want "the whole DM is one session," we'd derive `thread_ts`
from the channel id instead.)

## What `/reset` does

Drops the `claude_sessions` row for this thread. Next message in the
thread starts a fresh session — no prior context, no cache. The
on-disk session file claude wrote earlier is still there (we don't
delete it), so in principle you could grep for it. The audit
`messages` table is also untouched.

Trigger via:
- `/reset` (typed as a message, anywhere in a thread)
- `reset` / `clear` / `new` / `new chat` / `forget` (plain text shortcuts)

We don't register a native Slack slash command for `/reset` because
Slack slash commands aren't thread-aware (no `thread_ts` in the payload).
Typing `reset` as a message goes through `handle_user_query` which has
`thread_ts`, so it works.

## Failure mode: session expired

If the on-disk session file is missing or corrupt (claude clean-up,
project path moved, manual deletion), `--resume <id>` fails with
`No conversation found with session ID: …`. The bridge catches this
specifically (`SessionExpired` exception), clears the stale row,
retries without `--resume`, and gets a brand new session. The user
sees a fresh response — they lose continuity, but the bridge doesn't
crash on them.

## Cost / performance notes

- The `--output-format json` overhead is negligible (~10ms parsing).
- `cache_read_input_tokens` is logged on every call. Watch this in
  `bridge.log` to spot regressions or sessions that aren't reusing
  cache properly.
- Auto-compaction: claude transparently compacts long sessions when
  they hit context limits. The session_id stays the same; we don't need
  to do anything. (We're NOT passing `--fork-session`, which would mint
  a new id on resume.)
- Project path matters. Claude Code keys sessions by sanitized cwd
  (`~/.claude/projects/<repo-path>/`). The bridge always cd's to
  `REPO_DIR`, so as long as that doesn't move, sessions resolve. Moving
  the repo (e.g. clone to a different path on Brev) invalidates *all*
  prior sessions — they'll all hit the SessionExpired path on first
  resume after the move and start fresh. Acceptable.

## Tradeoffs vs. injecting history into prompts

| Aspect | This (--resume) | Inject history |
|---|---|---|
| Token cost / turn | input tokens for new turn only | full transcript every turn |
| Cache hits | yes — system prompt, tools | no — prompt is different each turn |
| Code complexity | one DB table, ~50 LoC | format/serialize history, ~100 LoC |
| Breaks if claude updates | yes — depend on CLI flag stability | no |
| Visible to dashboard | needs a separate read of `messages` | natural side effect |
| /reset semantics | drop one DB row | nothing to drop |

We chose `--resume` because the cost win compounds for long sessions
and the complexity stays small.

## Out of scope (parking lot)

- **Per-channel session instead of per-thread.** Would let users have
  one continuous DM conversation without using Slack threads. Possible
  later by deriving `thread_ts` from `channel_id` for top-level
  messages. Not implemented — keep current behavior simple.
- **Session listing / browsing UI.** Phase 4 dashboard could enumerate
  `claude_sessions` and let the user resume a past thread by clicking.
- **TTL / GC.** Sessions accumulate forever. Eventually we'll want a
  "drop sessions older than 30 days" job. Not urgent — the on-disk
  files are tiny and the table is tinier.
- **Cross-thread context handoff.** If you start a brief in thread A,
  then `/reset` and want to continue in thread B with the same context,
  we'd need a `--fork-session --session-id <new>` recipe. Not needed yet.
