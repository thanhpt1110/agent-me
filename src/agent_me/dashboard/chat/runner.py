"""Chat runner — a thin shim over the shared claude engine.

Spawning ``claude`` and parsing its NDJSON stream now live in
:mod:`agent_me.claude_agent.runner` (shared with the Slack bridge and the brief).
This module keeps the chat-facing names so :mod:`.service` and the tests are
unchanged:

* :class:`ClaudeRunner` — the Protocol the service depends on (DIP seam).
* :class:`ClaudeCliRunner` — the production runner, the shared
  :class:`~agent_me.claude_agent.runner.ClaudeAgentRunner` constructed with a
  ``ChatConfig`` (it exposes the same ``claude_bin``/``tool_result_max``/
  ``turn_timeout_s`` attributes the runner reads).
"""

from __future__ import annotations

from agent_me.claude_agent.runner import ClaudeAgentRunner, ClaudeRunner, OnEvent

__all__ = ["ClaudeCliRunner", "ClaudeRunner", "OnEvent"]

# The chat feature spawns the shared engine with chat defaults
# (--dangerously-skip-permissions, no tool scoping, no extra system prompt).
ClaudeCliRunner = ClaudeAgentRunner
