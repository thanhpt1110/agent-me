# dashboard — Starlette web app (entry: `uv run agent-me-dashboard`)

Server-rendered Jinja2 + Alpine.js + Tailwind (CDN), SSE via sse-starlette.
Reads the bridge's SQLite read-only; never writes bridge state.

- `app.py` — routes + thin handlers; `AuthMiddleware` login gate; operator-passcode
  gate (`DASHBOARD_OPERATOR_ACTION_CODE`) for operator actions AND `/chat`.
- `chat/` — native Chat feature (has its own CLAUDE.md).
- `auto_sfa_*` + `/mcp/` — Auto SFA UI + Streamable-HTTP MCP endpoint.
- `templates/` — **`x-data` attributes must be SINGLE-quoted.** `tojson` emits raw
  `"`; a double-quoted attribute truncates and Alpine never initialises (dead UI).
  Guarded by `tests/test_chat_gate.py::test_chat_xdata_attribute_is_well_formed`.

Test UI behavior in a real browser (`scripts/verify_chat_ui.py`), not just curl.
See repo-root `CLAUDE.md` for conventions.
