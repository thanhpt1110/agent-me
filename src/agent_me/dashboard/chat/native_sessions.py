"""Read the local ``claude`` CLI's own session store — the sessions
``claude --resume`` lists — so the web ``/chat`` shows terminal- and Slack-born
sessions, not only ones started in the browser.

Every surface that runs ``claude`` in the agent-me workspace (the dashboard, the
Slack bridge, an interactive terminal) writes one ``<session-id>.jsonl`` under
``~/.claude/projects/<cwd-slug>/``. This module is a pure reader: it lists those
files and parses one into the web chat's event vocabulary for display. Live
process state comes from ``~/.claude/sessions/*.json`` so a session currently
open elsewhere is shown read-only (resuming a live session would fork it).
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from agent_me.claude_agent.models import EventKind, now_ms

from .local_sessions import project_dir_name

# JSONL line types that carry a conversation turn; all others (mode,
# permission-mode, file-history-snapshot, attachment, summary, ai-title, system,
# queue-operation, last-prompt) are metadata mined for the title but not shown.
_TURN_TYPES = frozenset({"user", "assistant"})


def _iso_ms(value: object) -> int | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp() * 1000)
    except ValueError:
        return None


def _pid_alive(pid: object) -> bool:
    try:
        os.kill(int(pid), 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except (OSError, TypeError, ValueError):
        return False
    return True


def _first_text(content: object) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                return str(block.get("text") or "")
    return ""


@dataclass(slots=True)
class NativeSession:
    """One CLI session summarised from its on-disk ``<id>.jsonl``."""

    id: str
    title: str
    cwd: str
    git_branch: str | None
    created_at: int
    last_activity: int
    turns: int
    entrypoint: str | None
    live: bool = False
    busy: bool = False

    @property
    def origin(self) -> str:
        # Any claude session on this host that wasn't web- or Slack-born is a
        # "terminal" session — both interactive (`entrypoint=cli`) and headless
        # runs are surfaced the same way (the cli/terminal split only confused).
        return "terminal"

    def public_dict(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "cwd": self.cwd,
            "model": None,
            "claude_session_id": self.id,
            "claude_session_ids": [self.id],
            "claude_started": True,
            "created_at": self.created_at,
            "last_activity": self.last_activity,
            "turns": self.turns,
            "origin": self.origin,
            "slack_thread_ts": None,
            "native": True,
            "entrypoint": self.entrypoint,
            "live": self.live,
            "busy": self.busy,
        }


class NativeSessionReader:
    """Lists/parses the CLI's per-project session transcripts (read-only).

    Summaries are cached by ``(mtime, size)`` so the dashboard's periodic
    session-list poll re-parses only files that actually changed.
    """

    def __init__(self, projects_dir: str | os.PathLike[str], *, tool_result_max: int = 8000) -> None:
        self._projects_dir = Path(projects_dir)
        self._claude_home = self._projects_dir.parent
        self._tool_result_max = tool_result_max
        self._cache: dict[str, tuple[float, int, NativeSession | None]] = {}

    def _slug_dir(self, cwd: str) -> Path:
        return self._projects_dir / project_dir_name(cwd)

    def _path(self, cwd: str, sid: str) -> Path:
        return self._slug_dir(cwd) / f"{sid}.jsonl"

    # -- listing --------------------------------------------------------------
    def list_sessions(self, cwd: str) -> list[NativeSession]:
        slug = self._slug_dir(cwd)
        if not slug.is_dir():
            return []
        live = self._live_map()
        out: list[NativeSession] = []
        for path in slug.glob("*.jsonl"):
            sess = self._load(path.stem, path)
            if sess is None:
                continue
            info = live.get(sess.id)
            sess.live = info is not None
            sess.busy = bool(info and info.get("status") == "busy")
            out.append(sess)
        out.sort(key=lambda s: s.last_activity, reverse=True)
        return out

    def find(self, cwd: str, sid: str) -> NativeSession | None:
        path = self._path(cwd, sid)
        sess = self._load(sid, path)
        if sess is None:
            return None
        info = self._live_map().get(sid)
        sess.live = info is not None
        sess.busy = bool(info and info.get("status") == "busy")
        return sess

    def forget(self, sid: str) -> None:
        """Drop a cached summary (after the session's file is deleted)."""
        self._cache.pop(sid, None)

    def live_ids(self) -> set[str]:
        """Session ids currently held by a live (pid-alive) ``claude`` process —
        e.g. one open in a terminal. The web must not ``--resume`` these (it would
        conflict with / fork the running session)."""
        return set(self._live_map().keys())

    def _load(self, sid: str, path: Path) -> NativeSession | None:
        try:
            st = path.stat()
        except OSError:
            return None
        cached = self._cache.get(sid)
        if cached and cached[0] == st.st_mtime and cached[1] == st.st_size:
            return cached[2]
        sess = self._summarize(sid, path, st)
        self._cache[sid] = (st.st_mtime, st.st_size, sess)
        return sess

    def _summarize(self, sid: str, path: Path, st: os.stat_result) -> NativeSession | None:
        ai_title = summary = first_user = ""
        cwd = git_branch = entrypoint = None
        first_ts = last_ts = None
        turns = 0
        try:
            with path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        d = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    t = d.get("type")
                    if t == "ai-title":
                        ai_title = d.get("aiTitle") or d.get("title") or ai_title
                        continue
                    if t == "summary":
                        summary = d.get("summary") or summary
                        continue
                    if t not in _TURN_TYPES or d.get("isSidechain"):
                        continue
                    cwd = cwd or d.get("cwd")
                    git_branch = git_branch or d.get("gitBranch")
                    entrypoint = entrypoint or d.get("entrypoint")
                    ts = _iso_ms(d.get("timestamp"))
                    if ts is not None:
                        first_ts = first_ts if first_ts is not None else ts
                        last_ts = ts
                    if t == "user":
                        content = (d.get("message") or {}).get("content")
                        text = _first_text(content)
                        if text:
                            first_user = first_user or text
                            turns += 1
        except OSError:
            return None
        if turns == 0 and not ai_title and not summary:
            return None
        title = (ai_title or summary or first_user or "Untitled session").strip()
        mtime_ms = int(st.st_mtime * 1000)
        return NativeSession(
            id=sid,
            title=" ".join(title.split())[:120] or "Untitled session",
            cwd=cwd or str(self._projects_dir),
            git_branch=git_branch,
            created_at=first_ts if first_ts is not None else int(st.st_ctime * 1000),
            last_activity=last_ts if last_ts is not None else mtime_ms,
            turns=turns,
            entrypoint=entrypoint,
        )

    # -- transcript -----------------------------------------------------------
    def read_events(self, cwd: str, sid: str, *, cap: int = 0) -> list[dict] | None:
        """The session's conversation as web chat events (``kind``/``seq``).

        ``seq`` is numbered over the *full* history, then the last ``cap`` are
        returned (``cap=0`` → all). Keeping the true seq on the tail lets an
        adopted session's imported transcript dedup against this replay.
        """
        path = self._path(cwd, sid)
        if not path.exists():
            return None
        events: list[dict] = []
        seq = 0
        try:
            with path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        d = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if d.get("type") not in _TURN_TYPES or d.get("isSidechain"):
                        continue
                    for evt in self._line_events(d):
                        seq += 1
                        evt["seq"] = seq
                        events.append(evt)
        except OSError:
            return None
        return events[-cap:] if cap and len(events) > cap else events

    def read_events_tail(self, cwd: str, sid: str, limit: int, *, max_bytes: int = 1_000_000) -> list[dict] | None:
        """The last ``limit`` events of a native session as web events, read from
        the file tail only (bounded by ``max_bytes``) so opening a long terminal
        session stays smooth. Seq is local to the window (the read-only preview is
        self-contained; the client resets when the session is adopted)."""
        path = self._path(cwd, sid)
        try:
            size = path.stat().st_size
            with path.open("rb") as fh:
                start = max(0, size - max_bytes) if max_bytes else 0
                fh.seek(start)
                data = fh.read()
        except OSError:
            return None
        if start > 0:                      # drop the partial first line we cut into
            nl = data.find(b"\n")
            data = data[nl + 1:] if nl >= 0 else b""
        events: list[dict] = []
        for raw in data.split(b"\n"):
            raw = raw.strip()
            if not raw:
                continue
            try:
                d = json.loads(raw.decode("utf-8", "replace"))
            except json.JSONDecodeError:
                continue
            if d.get("type") not in _TURN_TYPES or d.get("isSidechain"):
                continue
            events.extend(self._line_events(d))
        window = events[-limit:] if limit and len(events) > limit else events
        for i, evt in enumerate(window, 1):
            evt["seq"] = i
        return window

    def _line_events(self, d: dict):
        ts = _iso_ms(d.get("timestamp")) or now_ms()
        message = d.get("message") or {}
        content = message.get("content")
        if d.get("type") == "user":
            if isinstance(content, str):
                if content.strip():
                    yield {"kind": EventKind.USER.value, "t": ts, "text": content}
                return
            for block in content or []:
                if not isinstance(block, dict):
                    continue
                if block.get("type") == "text" and block.get("text"):
                    yield {"kind": EventKind.USER.value, "t": ts, "text": block["text"]}
                elif block.get("type") == "tool_result":
                    yield self._tool_result_event(block, ts)
            return
        for block in content or []:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "text" and block.get("text"):
                yield {"kind": EventKind.TEXT.value, "t": ts, "text": block["text"]}
            elif btype == "thinking" and block.get("thinking"):
                yield {"kind": EventKind.THINKING.value, "t": ts, "text": block["thinking"]}
            elif btype == "tool_use":
                yield {"kind": EventKind.TOOL_USE.value, "t": ts,
                       "id": block.get("id"), "name": block.get("name"), "input": block.get("input")}

    def _tool_result_event(self, block: dict, ts: int) -> dict:
        content = block.get("content")
        if isinstance(content, list):
            content = "".join(c.get("text", "") for c in content if isinstance(c, dict))
        content = str(content if content is not None else "")
        cap = self._tool_result_max
        if cap and len(content) > cap:
            content = content[:cap] + f"\n…[truncated {len(content) - cap} chars]"
        return {"kind": EventKind.TOOL_RESULT.value, "t": ts,
                "id": block.get("tool_use_id"), "content": content,
                "is_error": bool(block.get("is_error"))}

    # -- live process state ---------------------------------------------------
    def _live_map(self) -> dict[str, dict]:
        sessions_dir = self._claude_home / "sessions"
        if not sessions_dir.is_dir():
            return {}
        out: dict[str, dict] = {}
        for path in sessions_dir.glob("*.json"):
            try:
                d = json.loads(path.read_text("utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            sid = d.get("sessionId")
            if not sid or not _pid_alive(d.get("pid")):
                continue
            out[sid] = d
        return out
