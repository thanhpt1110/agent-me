# 2026-05-14 - Dashboard, brief, and UI state

## User requests captured

This session focused on making the web dashboard match the behavior of
the Slack `brief` command while improving operator UX:

- Slack `brief` fetches should update the dashboard cache, not only Slack
  DM/thread output.
- Overview and source detail pages should show last update timestamps for
  auditability.
- Each platform should have its own refresh action, guarded by Thanh
  Phan's operator passcode.
- A browser that already entered the correct operator passcode should be
  allowed to reuse operator actions without another popup.
- Meetings must show full start/end times, with a compact dashboard view
  and richer source detail view.
- Platform order and labels should be normalized:
  `NVBugs`, `GitLab`, `GitHub`, `Meetings`, `Email`, `Jira`, `Teams`,
  `Slack`.
- Microsoft Teams chat should become a first-class brief/dashboard source.
- Refresh/open/operation buttons should be smaller, clearer, and harder to
  misclick.
- The whole dashboard should support light/dark themes, defaulting to the
  user's OS/browser theme.

## Implemented state

- Added per-source refresh endpoints and dashboard controls. Refreshing all
  sources still exists, but the dashboard can now refresh one platform at a
  time.
- Added operator-token persistence in browser local storage. Once a valid
  passcode is accepted, refresh and MCP reauth flows reuse the issued token.
- Updated Slack brief flow so fetched source data is written back to the
  dashboard cache.
- Added Teams as a source in source ordering, dashboard rendering, brief
  fanout, and tests.
- Renamed user-facing source labels: `Outlook Calendar` -> `Meetings`,
  `Outlook` -> `Email`, and Teams is shown as `Teams`.
- Meetings overview now renders compact columns: meeting title, start, end.
  Detail pages keep full meeting metadata but remove timezone/location noise
  and show relative timing instead of raw busy text.
- Added last-updated display on Overview and source detail pages.
- Reworked refresh/MCP auth/ops buttons for clearer size, text, and icons.
- Added system-aware light/dark theme support with a navbar toggle. Manual
  theme choices persist per browser; without an override, the UI follows
  `prefers-color-scheme`.
- Hardened light theme contrast, including user-visible text, placeholders,
  Auto SFA notes, cancel buttons, and the Auto SFA terminal.
- Removed the redundant Auto SFA terminal `clear` action. The remaining
  terminal action is a `+` button for opening a new terminal session.

## Verification

The following checks passed after the changes:

```text
uv run ruff check tests/test_app.py
uv run pytest -q tests/test_app.py
uv run pytest -q
```

After verification, both user services were restarted successfully:

```text
agent-me-dashboard.service: active
agent-me-bridge.service: active
http://127.0.0.1:8765/healthz: ok
```

## Notes for future work

- There is an older dev dashboard process on port `8766`. The production
  user service is on `8765`.
- The current frontend still uses Tailwind CDN plus a small custom CSS
  layer. If this grows much further, moving to compiled CSS would make
  theme-specific rules easier to maintain.
