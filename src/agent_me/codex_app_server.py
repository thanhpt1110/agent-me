"""Codex app-server helper for permissioned connector/MCP writes.

Use this path when the agent must call a connector or MCP write tool from an
automated runtime. Headless `codex exec` is still the default for reads, but
some app connector writes need Codex app-server's auto-review flow to approve
and execute the tool call.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any

from agent_me.mcp_tokens import codex_mcp_token_env

APP_SERVER_AUTO_REVIEW_CONFIGS = (
    'approval_policy="on-request"',
    'approvals_reviewer="auto_review"',
)


def codex_app_server_args(codex_bin: str, prompt: str) -> list[str]:
    args = [codex_bin]
    for cfg in APP_SERVER_AUTO_REVIEW_CONFIGS:
        args.extend(["-c", cfg])
    args.extend(["debug", "app-server", "send-message-v2", prompt])
    return args


def parse_debug_json_blocks(output: str) -> list[dict[str, Any]]:
    """Extract JSON-RPC objects from `codex debug app-server` transcript output."""
    blocks: list[dict[str, Any]] = []
    lines = output.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if not line.startswith("< {"):
            i += 1
            continue
        chunk: list[str] = []
        while i < len(lines) and lines[i].startswith("< "):
            chunk.append(lines[i][2:])
            try:
                obj = json.loads("\n".join(chunk))
            except json.JSONDecodeError:
                i += 1
                continue
            if isinstance(obj, dict):
                blocks.append(obj)
            i += 1
            break
        else:
            i += 1
    return blocks


def parse_app_server_final_message(output: str) -> str | None:
    final: str | None = None
    for obj in parse_debug_json_blocks(output):
        if obj.get("method") != "item/completed":
            continue
        item = ((obj.get("params") or {}).get("item") or {})
        if item.get("type") == "agentMessage":
            text = (item.get("text") or "").strip()
            if text:
                final = text
    return final


async def run_codex_app_server(
    prompt: str,
    *,
    codex_bin: str,
    cwd: Path,
    timeout_s: float,
    env: dict[str, str] | None = None,
) -> str:
    args = codex_app_server_args(codex_bin, prompt)
    spawn_env = os.environ.copy()
    spawn_env.update(codex_mcp_token_env())
    if env:
        spawn_env.update(env)

    proc = await asyncio.create_subprocess_exec(
        *args,
        cwd=str(cwd),
        env=spawn_env,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        limit=32 * 1024 * 1024,
    )
    try:
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(), timeout=timeout_s,
        )
    except TimeoutError as exc:
        proc.kill()
        await proc.wait()
        raise RuntimeError(f"codex app-server timed out after {timeout_s}s") from exc

    stdout_text = stdout.decode(errors="replace")
    stderr_text = stderr.decode(errors="replace").strip()
    if proc.returncode != 0:
        err = (stderr_text or stdout_text)[-1000:]
        raise RuntimeError(f"codex app-server exited {proc.returncode}: {err}")

    final = parse_app_server_final_message(stdout_text)
    if not final:
        tail = (stdout_text + "\n" + stderr_text)[-1000:]
        raise RuntimeError(f"codex app-server returned no final message: {tail}")
    return final
