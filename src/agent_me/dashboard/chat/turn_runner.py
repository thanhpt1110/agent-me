"""Detached turn-runner: the process that actually runs a web chat session's turns.

Why a separate process: the dashboard gets restarted/redeployed, and a turn run as
its child would be SIGTERM-killed mid-flight (the "exit code 143" the operator hit).
The dashboard instead launches this worker in its **own** process group / cgroup
(``systemd-run --scope`` or ``setsid``), so a dashboard restart leaves it running.
The worker drains the session's durable queue (``meta.pending``), streams events into
the on-disk transcript, and the dashboard just **tails** that transcript — so it can
disconnect, restart, and reconnect to the still-running turn without killing it.

Single-writer: only one worker drains a session at a time (guarded by ``running_pid``
+ a flock'd queue), so a plain seq counter seeded from the transcript stays monotonic.
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

from agent_me.claude_agent.models import EventKind, make_event

from .config import ChatConfig
from .runner import ClaudeCliRunner
from .store import DiskSessionStore

# Exit codes meaning the run was signalled (restart/OOM), not a real claude error:
# 143/137 = 128+SIGTERM/SIGKILL; -15/-9 = asyncio's raw signal form.
SIGNAL_EXIT_CODES = frozenset({143, -15, 137, -9})
MAX_PENDING_ATTEMPTS = 3   # give up auto-resuming a message that keeps getting killed


class TurnExecutor:
    """Drains one session's queued messages, writing events to the store. Injectable
    (store/runner/config) so the drain logic is unit-tested in-process without spawning
    the real worker."""

    def __init__(self, store: DiskSessionStore, runner, config: ChatConfig) -> None:
        self._store = store
        self._runner = runner
        self._config = config
        self._seq = 0

    def _emit(self, sid: str, event: dict) -> dict:
        self._seq += 1
        event["seq"] = self._seq
        self._store.append_event(sid, event)
        return event

    async def run(self, sid: str) -> None:
        meta = self._store.get_meta(sid)
        if meta is None:
            return
        # Take ownership. The launch was already serialized by ``store.claim_run``
        # (which set running_pid to the *launcher's* pid as a placeholder), so we
        # just overwrite it with our own — no extra guard, or we'd mistake that
        # placeholder for "another worker" and exit immediately.
        self._store.update_meta(sid, running_pid=os.getpid())
        self._seq = self._store.last_seq(sid)
        try:
            while True:
                meta = self._store.get_meta(sid)
                if meta is None or not meta.pending:
                    break
                if not await self._run_one(sid, meta.pending[0], meta):
                    break   # signal-interrupted: leave it pending so it resumes
        finally:
            # Release ownership and close the turn so a tailing client stops waiting.
            self._store.update_meta(sid, running_pid=0)
            self._emit(sid, make_event(EventKind.TURN_END))

    async def _run_one(self, sid: str, text: str, meta) -> bool:
        """Run a single queued message. Returns True if it reached a terminal state
        (pop it), False if signal-interrupted (keep it for resume)."""
        self._emit(sid, make_event(EventKind.USER, text=text))
        if (not meta.title or meta.title == "New chat") and meta.turns == 0:
            self._store.update_meta(sid, title=(" ".join(text.split())[:60] or "Chat"))

        try:
            result = await self._runner.run_turn(
                prompt=text, cwd=meta.cwd, session_id=meta.claude_session_id,
                resume=meta.claude_started, model=meta.model or self._config.model,
                add_dirs=self._config.add_dirs, on_event=lambda e: self._emit(sid, e),
                append_system_prompt=self._config.append_system_prompt,
            )
        except Exception as exc:   # surface any spawn/parse failure; don't crash the worker
            self._emit(sid, make_event(EventKind.ERROR, text=f"Turn failed: {exc}"))
            self._store.pop_pending(sid)
            return True

        signalled = bool(result.is_error and result.exit_code in SIGNAL_EXIT_CODES)
        new_sid = result.session_id or meta.claude_session_id
        seen = list(dict.fromkeys(
            s for s in [*meta.claude_session_ids, meta.claude_session_id, new_sid] if s))
        updates: dict = dict(
            claude_session_id=new_sid, claude_session_ids=seen,
            claude_started=True, turns=meta.turns + (0 if signalled else 1))
        if result.context_tokens:  # a killed/empty turn reports 0 — keep the prior gauge
            updates["context_tokens"] = result.context_tokens
        self._store.update_meta(sid, **updates)

        if signalled:
            attempts = (self._store.get_meta(sid).pending_attempts or 0) + 1
            if attempts >= MAX_PENDING_ATTEMPTS:
                self._store.pop_pending(sid)   # poison guard: stop retrying
                self._emit(sid, make_event(EventKind.ERROR,
                    text="⚠️ Giving up after repeated interruptions — please send your message again."))
                return True
            self._store.update_meta(sid, pending_attempts=attempts)
            self._emit(sid, make_event(EventKind.ERROR,
                text="⚠️ Turn interrupted (server restart) — it will resume automatically."))
            return False

        if result.is_error:
            last = next((ln for ln in reversed((result.stderr or "").splitlines()) if ln.strip()), "")
            self._emit(sid, make_event(EventKind.ERROR,
                text=f"claude exited with an error (code {result.exit_code}). {last[:300]}".rstrip()))
        self._store.pop_pending(sid)
        return True


def main() -> None:
    from dotenv import load_dotenv

    if len(sys.argv) < 2:
        raise SystemExit("usage: agent-me-chat-turn <session-id>")
    sid = sys.argv[1]
    repo = os.environ.get("AGENT_ME_REPO_DIR") or str(Path(__file__).resolve().parents[4])
    load_dotenv(Path(repo) / "configs" / ".env")
    config = ChatConfig.from_env(repo)
    executor = TurnExecutor(DiskSessionStore(config.data_dir), ClaudeCliRunner(config), config)
    asyncio.run(executor.run(sid))


if __name__ == "__main__":
    main()
