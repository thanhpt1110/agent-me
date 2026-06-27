"""Stream one turn from the local ``claude`` CLI and map its NDJSON to events."""
from __future__ import annotations

import asyncio
import codecs
import contextlib
import json
import os
import signal
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol

from .models import EventKind, TurnResult, make_event

OnEvent = Callable[[dict], None]


def _noop(_event: dict) -> None:
    pass


def _signal_process_group(proc: asyncio.subprocess.Process, sig: int) -> None:
    if proc.returncode is not None:
        return
    try:
        os.killpg(os.getpgid(proc.pid), sig)
    except (ProcessLookupError, PermissionError):
        with contextlib.suppress(ProcessLookupError):
            proc.send_signal(sig)


@dataclass(frozen=True, slots=True)
class RunnerConfig:
    claude_bin: str = "claude"
    tool_result_max: int = 8000
    turn_timeout_s: float = 0.0


class _RunnerConfigLike(Protocol):
    claude_bin: str
    tool_result_max: int
    turn_timeout_s: float


class ClaudeRunner(Protocol):
    async def run_turn(
        self,
        *,
        prompt: str,
        cwd: str,
        session_id: str | None,
        resume: bool,
        model: str | None,
        add_dirs: tuple[str, ...],
        on_event: OnEvent,
    ) -> TurnResult: ...


class ClaudeAgentRunner:
    def __init__(self, config: _RunnerConfigLike | None = None) -> None:
        self._cfg = config or RunnerConfig()

    def _build_args(
        self, *, prompt: str, session_id: str | None, resume: bool,
        model: str | None, add_dirs: Sequence[str],
        allowed_tools: Sequence[str] | None,
        disallowed_tools: Sequence[str] | None,
        system_prompt: str | None,
        append_system_prompt: str | None,
        permission_mode: str | None,
        extra_args: Sequence[str],
    ) -> list[str]:
        args = [self._cfg.claude_bin, "-p", prompt,
                "--output-format", "stream-json", "--verbose"]
        if permission_mode:
            args += ["--permission-mode", permission_mode]
        else:
            args += ["--dangerously-skip-permissions"]
        if resume and session_id:
            args += ["--resume", session_id]
        elif session_id:
            # Pin the id on the first turn so an interrupted turn stays resumable.
            args += ["--session-id", session_id]
        if model:
            args += ["--model", model]
        for d in add_dirs:
            args += ["--add-dir", d]
        # --allowedTools is variadic: pass one comma-joined token so a following
        # flag is never swallowed.
        if allowed_tools:
            args += ["--allowedTools", ",".join(allowed_tools)]
        if disallowed_tools:
            args += ["--disallowedTools", ",".join(disallowed_tools)]
        if system_prompt:
            args += ["--system-prompt", system_prompt]
        if append_system_prompt:
            args += ["--append-system-prompt", append_system_prompt]
        args += list(extra_args)
        return args

    async def run_turn(
        self, *, prompt: str, cwd: str, session_id: str | None = None,
        resume: bool = False, model: str | None = None,
        add_dirs: Sequence[str] = (), on_event: OnEvent | None = None,
        allowed_tools: Sequence[str] | None = None,
        disallowed_tools: Sequence[str] | None = None,
        system_prompt: str | None = None,
        append_system_prompt: str | None = None,
        permission_mode: str | None = None,
        extra_args: Sequence[str] = (),
        env: Mapping[str, str] | None = None,
    ) -> TurnResult:
        emit = on_event or _noop
        args = self._build_args(
            prompt=prompt, session_id=session_id, resume=resume, model=model,
            add_dirs=add_dirs, allowed_tools=allowed_tools,
            disallowed_tools=disallowed_tools, system_prompt=system_prompt,
            append_system_prompt=append_system_prompt,
            permission_mode=permission_mode, extra_args=extra_args,
        )
        spawn_env = {**os.environ, **env} if env else None
        try:
            proc = await asyncio.create_subprocess_exec(
                *args, cwd=cwd, env=spawn_env,
                stdin=asyncio.subprocess.DEVNULL,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                limit=16 * 1024 * 1024,
                start_new_session=True,  # group leader so terminate reaches child tools
            )
        except (OSError, ValueError) as exc:
            emit(make_event(EventKind.ERROR, text=f"Failed to launch claude: {exc}"))
            return TurnResult(session_id=session_id, is_error=True, stderr=str(exc))

        state = {"sid": session_id, "result": "", "is_error": False}

        async def pump_stdout() -> None:
            decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
            buf = ""
            while True:
                chunk = await proc.stdout.read(4096)
                if not chunk:
                    buf += decoder.decode(b"", final=True)
                    if buf.strip():
                        self._handle_line(buf, state, emit)
                    break
                buf += decoder.decode(chunk)
                while "\n" in buf:
                    line, buf = buf.split("\n", 1)
                    if line.strip():
                        self._handle_line(line, state, emit)

        async def read_stderr() -> bytes:
            return await proc.stderr.read()

        stderr_task = asyncio.ensure_future(read_stderr())
        try:
            timeout = self._cfg.turn_timeout_s or None
            async with asyncio.timeout(timeout):
                await pump_stdout()
                rc = await proc.wait()
            stderr_bytes = await stderr_task
        except TimeoutError:
            await self._terminate(proc)
            stderr = await self._collect_stderr(stderr_task)
            emit(make_event(EventKind.ERROR, text=f"Turn exceeded {self._cfg.turn_timeout_s}s; terminated."))
            return TurnResult(session_id=state["sid"], is_error=True, exit_code=-1, stderr=stderr)
        except asyncio.CancelledError:
            await self._terminate(proc)
            stderr_task.cancel()
            raise
        finally:
            with contextlib.suppress(Exception):
                if proc.returncode is None:
                    await self._terminate(proc)

        stderr = (stderr_bytes or b"").decode("utf-8", "replace")[-16000:]
        return TurnResult(
            session_id=state["sid"], result=state["result"],
            is_error=bool(state["is_error"] or rc != 0), exit_code=rc, stderr=stderr,
        )

    def _handle_line(self, line: str, state: dict, on_event: OnEvent) -> None:
        try:
            evt = json.loads(line)
        except json.JSONDecodeError:
            return
        if evt.get("session_id"):
            state["sid"] = evt["session_id"]
        etype = evt.get("type")
        try:
            if etype == "system" and evt.get("subtype") == "init":
                on_event(make_event(
                    EventKind.INIT, session_id=evt.get("session_id"),
                    model=evt.get("model"), cwd=evt.get("cwd"), tools=evt.get("tools"),
                ))
            elif etype == "assistant":
                for block in (evt.get("message") or {}).get("content") or []:
                    self._emit_assistant_block(block, on_event)
            elif etype == "user":
                for block in (evt.get("message") or {}).get("content") or []:
                    if block.get("type") == "tool_result":
                        on_event(self._tool_result_event(block))
            elif etype == "result":
                state["is_error"] = bool(evt.get("is_error")) or evt.get("subtype") != "success"
                state["result"] = evt.get("result") or ""
                on_event(make_event(
                    EventKind.RESULT, subtype=evt.get("subtype"), result=state["result"],
                    is_error=state["is_error"], cost_usd=evt.get("total_cost_usd"),
                    duration_ms=evt.get("duration_ms"),
                ))
        except Exception:
            return

    def _emit_assistant_block(self, block: dict, on_event: OnEvent) -> None:
        btype = block.get("type")
        if btype == "text" and block.get("text"):
            on_event(make_event(EventKind.TEXT, text=block["text"]))
        elif btype == "thinking" and block.get("thinking"):
            on_event(make_event(EventKind.THINKING, text=block["thinking"]))
        elif btype == "tool_use":
            on_event(make_event(
                EventKind.TOOL_USE, id=block.get("id"), name=block.get("name"), input=block.get("input"),
            ))

    def _tool_result_event(self, block: dict) -> dict:
        content = block.get("content")
        if isinstance(content, list):
            content = "".join(c.get("text", "") for c in content if isinstance(c, dict))
        content = str(content if content is not None else "")
        cap = self._cfg.tool_result_max
        if len(content) > cap:
            content = content[:cap] + f"\n…[truncated {len(content) - cap} chars]"
        return make_event(
            EventKind.TOOL_RESULT, id=block.get("tool_use_id"),
            content=content, is_error=bool(block.get("is_error")),
        )

    @staticmethod
    async def _collect_stderr(stderr_task: asyncio.Future) -> str:
        try:
            data = await asyncio.wait_for(stderr_task, timeout=3.0)
        except (TimeoutError, asyncio.CancelledError, Exception):
            stderr_task.cancel()
            return ""
        return (data or b"").decode("utf-8", "replace")[-16000:]

    @staticmethod
    async def _terminate(proc: asyncio.subprocess.Process) -> None:
        _signal_process_group(proc, signal.SIGTERM)
        try:
            await asyncio.wait_for(proc.wait(), timeout=5.0)
        except TimeoutError:
            _signal_process_group(proc, signal.SIGKILL)
