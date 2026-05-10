"""Three log streams for the `/logs` page.

Each stream is an `AsyncIterator[dict]` with a uniform envelope:

    {"source": str, "line": <parsed-or-raw>, "ts": <ms>, "replay": bool}

`replay=True` distinguishes the initial "tail -n N" replay from live
follow lines, so the UI can choose to dim the replay block.

Three sources, three follow strategies:

1. **Watcher** — `journalctl --user -u <unit> -f`. We spawn the binary
   ourselves and stream stdout. journalctl exits 0 on SIGTERM so
   `proc.terminate()` is enough; no kill escalation needed for the
   short-running dashboard process.

2. **Slack interactions** — wrap `StateReader.tail_logs` (which already
   handles inode/size rotation) and filter by an event allowlist. The
   allowlist intentionally lives next to the filter rather than in
   `state_reader` because the *consumer* defines what counts as a
   "user-facing" interaction; bridge can keep emitting any event it
   wants.

3. **Claude session trace** — `~/.claude/projects/<sanitized>/<sid>.jsonl`.
   Claude Code itself writes these JSONL files line-by-line, but a
   reader that polls between two write syscalls can see a half-written
   line. We accumulate bytes and only emit on `\n` boundaries, holding
   the trailing partial in a buffer until the next poll.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import structlog

from agent_me.dashboard.state_reader import BRIDGE_LOG, STATE_DIR, StateReader

log = structlog.get_logger("dashboard.log_sources")


# Bridge events the user actually cares about for the "Slack interaction"
# tab. Everything outside this set (e.g. periodic heartbeat, internal
# wiring, brief refresh plumbing) is suppressed. Keep this in sync with
# the bridge's `log.info(...)` callsites; new buttons → add the event.
SLACK_INTERACTION_EVENTS: frozenset[str] = frozenset({
    # Inbound message handling
    "message_received",
    "query_handled",
    "query_failed",
    "plain_handled",
    "plain_failed",
    "slash_handled",
    "slash_failed",
    "native_slash",
    "native_slash_failed",
    # Buttons (interactive components)
    "button_brief_refresh",
    "button_brief_reauth",
    "button_morning_reauth",
    "button_morning_brief_now",
    "button_menu_brief_day",
    "button_menu_brief_week",
    "button_menu_brief_month",
    "button_menu_mcp_status",
    "button_menu_help",
    # Failure / soft-reject paths worth surfacing
    "message_rejected_user",
    "auto_discovered_operator",
    "post_thinking_failed",
    "error_update_failed",
    "reply_in_thread_no_channel",
    # Phase 2b approval-gate events (added 2026-05-10 alongside the
    # PreToolUse hook + Slack approve/reject buttons).
    "approval_posted",
    "approval_resolved",
    "approval_auto_allowed",
    "approval_auto_thread_on",
})


# ── 1. Watcher: journalctl --user -u <unit> ─────────────────────────────


async def tail_journal_unit(
    unit: str,
    from_lines: int = 80,
    follow: bool = True,
) -> AsyncIterator[dict[str, Any]]:
    """Spawn journalctl and stream lines until the consumer cancels.

    `from_lines` lines are emitted with `replay=True`; every line after
    that is `replay=False`. We don't try to parse lines as JSON — the
    watcher unit emits plain text via Python `print()`, so callers get
    them as-is in the `line` field.

    Cancellation behaviour: when the SSE consumer disconnects Starlette
    cancels the iterator's task. The `try/finally` makes sure the
    subprocess is terminated and reaped so we don't leak a journalctl
    per disconnected browser.
    """
    cmd = [
        "journalctl",
        "--user",
        "-u", unit,
        "-n", str(from_lines),
        "-o", "short-iso",
        "--no-hostname",
    ]
    if follow:
        cmd.append("-f")

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    except FileNotFoundError:
        yield {
            "source": "watcher",
            "line": {"event": "journalctl_unavailable",
                     "note": "journalctl binary not found on PATH"},
            "ts": int(time.time() * 1000),
            "replay": False,
        }
        return

    assert proc.stdout is not None  # PIPE was set above
    emitted = 0
    try:
        while True:
            raw = await proc.stdout.readline()
            if not raw:
                # journalctl exited (e.g. unit doesn't exist, or follow
                # was False and it streamed the tail then quit).
                break
            line = raw.decode(errors="replace").rstrip("\n")
            if not line:
                continue
            # The first `from_lines` of output are the historical tail;
            # journalctl prints them before starting to follow. We can't
            # tell exactly when "follow" begins from stdout alone, so
            # use the line count as a heuristic (good enough — UI just
            # uses it to grey-out the replay block).
            yield {
                "source": "watcher",
                "line": line,
                "ts": int(time.time() * 1000),
                "replay": emitted < from_lines,
            }
            emitted += 1
    finally:
        # Signal journalctl to exit, then reap. Bound the wait so a
        # stuck child doesn't pin the SSE coroutine forever.
        if proc.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                proc.terminate()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(proc.wait(), timeout=2.0)
            if proc.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
                with contextlib.suppress(Exception):
                    await proc.wait()


# ── 2. Slack interactions: filtered bridge.log tail ─────────────────────


async def tail_bridge_slack_filtered(
    from_lines: int = 50,
) -> AsyncIterator[dict[str, Any]]:
    """Tail `bridge.log` and emit only events in `SLACK_INTERACTION_EVENTS`.

    Reuses `StateReader.tail_logs` so we inherit its inode-aware
    rotation handling. We pass only `BRIDGE_LOG` (no brief.log) and
    drop anything whose decoded `event` field isn't in the allowlist.
    Lines that aren't valid JSON (the `tail_logs` fallback wraps them
    as `{"raw": ...}`) are also dropped — the slack tab only ever
    shows decoded structured events.
    """
    async for evt in StateReader.tail_logs(
        sources=(BRIDGE_LOG,),
        from_lines=from_lines,
    ):
        line = evt.get("line")
        if not isinstance(line, dict):
            continue
        if line.get("event") not in SLACK_INTERACTION_EVENTS:
            continue
        # Re-tag so the SSE client knows which tab this belongs to even
        # if multiple streams ever get multiplexed in one connection.
        yield {**evt, "source": "slack"}


# ── 3. Claude session trace: ~/.claude/projects/<sanitized>/<sid>.jsonl ─


CLAUDE_PROJECTS_DIR = Path.home() / ".claude" / "projects"


def _sanitize_path(path: Path) -> str:
    """Mirror Claude Code's project-dir naming.

    Claude Code replaces every non-alphanumeric character in the
    absolute path with `-`. Example:

        /localhome/local-thaphan/.local/state/agent-me/chat-cwd
        →
        -localhome-local-thaphan--local-state-agent-me-chat-cwd

    The `.` and `/` both collapse to `-`, which makes the encoding
    lossy (you can't recover the original from the dir name alone).
    For *resolution* that's fine — we only need the forward direction.
    """
    return re.sub(r"[^A-Za-z0-9]", "-", str(path))


def _decode_dir_name(name: str) -> str:
    """Inverse of `_sanitize_path` for fuzzy matching only.

    Used by the glob fallback to compare a dir basename to a path.
    The decode is lossy (`-` → `/` collapses adjacent dashes), so
    callers must compare via `_sanitize_path` round-trip rather than
    string equality on the decoded form.
    """
    return name.replace("-", "/")


def resolve_session_jsonl_path(session_id: str) -> Path | None:
    """Find `<session_id>.jsonl` in the project dir matching CHAT_CWD.

    Two-step lookup:

    1. Build the literal sanitized name from `STATE_DIR / "chat-cwd"`
       (after resolving symlinks). If `<projects>/<sanitized>/` exists
       and contains the session jsonl, return it.

    2. Glob `~/.claude/projects/*/` and pick the dir whose name
       round-trips to the same sanitized form. This catches drift if
       Claude Code ever changes its sanitizer (e.g. adds case-folding).

    Returns `None` if no matching jsonl exists.
    """
    if not CLAUDE_PROJECTS_DIR.exists():
        return None

    chat_cwd = STATE_DIR / "chat-cwd"
    try:
        chat_cwd_resolved = chat_cwd.resolve()
    except OSError:
        chat_cwd_resolved = chat_cwd

    expected_name = _sanitize_path(chat_cwd_resolved)
    direct = CLAUDE_PROJECTS_DIR / expected_name
    candidate = direct / f"{session_id}.jsonl"
    if candidate.is_file():
        return candidate

    # Glob fallback — handles the case where chat_cwd resolution
    # differs slightly (e.g. trailing slash, /private prefix on macOS)
    # but Claude's sanitizer agrees with ours.
    for project_dir in CLAUDE_PROJECTS_DIR.glob("*/"):
        if not project_dir.is_dir():
            continue
        # Compare via decoded-then-resanitized — protects against
        # edge cases where one side has a different absolute prefix
        # but the same fuzzy structure.
        decoded = _decode_dir_name(project_dir.name)
        if _sanitize_path(Path(decoded)) == expected_name:
            session_path = project_dir / f"{session_id}.jsonl"
            if session_path.is_file():
                return session_path

    return None


async def tail_session_jsonl(
    session_id: str,
    from_lines: int = 30,
    follow: bool = True,
    poll_interval_s: float = 0.5,
) -> AsyncIterator[dict[str, Any]]:
    """Tail a Claude session JSONL with partial-line safety.

    Claude Code writes each turn as a single JSON object terminated by
    `\\n`. Between two write syscalls the file may end mid-line; we
    accumulate bytes in a buffer and only emit when we've seen a
    complete `\\n`-terminated chunk. This prevents `JSONDecodeError`
    every time we poll while a turn is being written.

    If the session jsonl can't be found (wrong id, fresh session not
    flushed yet), we yield a single `missing` event and stop — caller
    can show "no trace yet".
    """
    path = resolve_session_jsonl_path(session_id)
    if path is None:
        yield {
            "source": "session",
            "line": {"event": "missing", "session_id": session_id,
                     "note": "no jsonl under ~/.claude/projects/.../"},
            "ts": int(time.time() * 1000),
            "replay": False,
        }
        return

    # Initial replay. Read raw bytes so we can split off any trailing
    # partial line and seed `pending` with it — that way the live loop
    # doesn't lose the bytes we already read, and the partial isn't
    # mis-emitted as a complete replay line.
    try:
        raw_bytes = path.read_bytes()
    except OSError as exc:
        log.warning("session_jsonl_read_failed", path=str(path), err=str(exc))
        raw_bytes = b""
    last_nl = raw_bytes.rfind(b"\n")
    if last_nl < 0:
        complete_bytes = b""
        pending = raw_bytes
    else:
        complete_bytes = raw_bytes[: last_nl + 1]
        pending = raw_bytes[last_nl + 1 :]
    offset = len(raw_bytes)

    replay_lines = complete_bytes.decode(errors="replace").splitlines()[-from_lines:]
    for raw in replay_lines:
        if not raw.strip():
            continue
        yield {
            "source": "session",
            "line": _try_parse_json(raw),
            "ts": int(time.time() * 1000),
            "replay": True,
        }

    if not follow:
        return

    # Live tail. Track byte offset and accumulate any trailing partial
    # in `pending` until we see a newline. This is the partial-write
    # fix: without it, polling between two writes can produce an
    # undecodable half-object that we'd otherwise yield as
    # `{"raw": ...}` garbage.
    while True:
        await asyncio.sleep(poll_interval_s)
        if not path.exists():
            continue
        try:
            cur_size = path.stat().st_size
        except OSError:
            continue
        if cur_size < offset:
            # Truncation/rotation — Claude Code doesn't normally rotate
            # session jsonls, but a manual `>` redirect would do this.
            offset = 0
            pending = b""
        if cur_size == offset:
            continue
        try:
            with path.open("rb") as f:
                f.seek(offset)
                chunk = f.read(cur_size - offset)
        except OSError:
            continue
        offset = cur_size
        pending += chunk
        # Split on newline; the last segment may be a partial line if
        # the file ends without a newline. Stash it for next iteration.
        *complete, pending_tail = pending.split(b"\n")
        pending = pending_tail
        for line_bytes in complete:
            raw = line_bytes.decode(errors="replace")
            if not raw.strip():
                continue
            yield {
                "source": "session",
                "line": _try_parse_json(raw),
                "ts": int(time.time() * 1000),
                "replay": False,
            }


# ── helpers ─────────────────────────────────────────────────────────────


def _try_parse_json(raw: str) -> dict[str, Any]:
    """Same fallback shape as `state_reader._try_parse_json`.

    Duplicated rather than imported because it's a 4-line helper and
    importing across modules just to share it would create a backward
    coupling we don't need.
    """
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return {"raw": raw}
