"""Unit tests for the shared claude engine's NDJSON → event mapping."""
from __future__ import annotations

import json

from agent_me.claude_agent.runner import ClaudeAgentRunner, _context_tokens


def test_context_tokens_sums_prompt_and_output() -> None:
    usage = {"input_tokens": 100, "cache_read_input_tokens": 900,
             "cache_creation_input_tokens": 50, "output_tokens": 200}
    assert _context_tokens(usage) == 1250
    assert _context_tokens({}) == 0          # no usage yet
    assert _context_tokens(None) == 0        # missing/garbage shapes are safe
    assert _context_tokens({"input_tokens": "x"}) == 0


def test_result_line_emits_context_tokens() -> None:
    """The CLI's ``result`` event carries ``usage``; the RESULT event must forward a
    summed ``context_tokens`` so the /chat % gauge has a value."""
    events: list[dict] = []
    line = json.dumps({
        "type": "result", "subtype": "success", "is_error": False,
        "session_id": "s1", "total_cost_usd": 0.01, "duration_ms": 1234,
        "usage": {"input_tokens": 10, "cache_read_input_tokens": 90, "output_tokens": 100},
    })
    ClaudeAgentRunner()._handle_line(line, {"sid": "s1", "result": "", "is_error": False,
                                            "context_tokens": 0}, events.append)
    result = next(e for e in events if e["kind"] == "result")
    assert result["context_tokens"] == 200
