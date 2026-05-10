"""agent-me dashboard — read-only web view over bridge state + brief output.

Phase 4. See `design/dashboard-design.md` for architecture, isolation
guarantees, and tunnel choice (Tailscale Funnel for the public URL).

The dashboard never writes to the bridge's state DB. It opens a separate
read-only SQLite connection per request and tails the bridge/brief logs.
On-demand refresh spawns its own `agent-me-brief` subprocess with
`--no-post --source <id>` so it doesn't double-post to Slack.
"""
