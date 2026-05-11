# Phase 4 — NVIDIA theme + Pending Tasks panel

**Status:** Shipped 2026-05-11. Live on `https://agent-me.nvidia.com`.
Code in `src/agent_me/dashboard/`. Mock data only — real-source
wiring is Phase 5.

## Goal

Make the Overview page actionable at a glance for a daily-driver
operator. Instead of jumping into Jira / GitLab / Confluence /
NVBugs / Slack / Outlook / GitHub tabs individually, surface every
pending item across all platforms (+ Slack threads + Claude sessions)
on the landing page, with one click to the upstream system.

Also re-theme the dashboard from the draft "blue accent on dark
slate" palette to the **official NVIDIA brand palette** (black +
`#76b900` green) so the dashboard visually matches the rest of the
NVIDIA-internal tooling on `*.nvidia.com`.

## What changed (TL;DR)

1. **Overview now shows 9 platform-group cards** above the existing
   "Briefs by source" grid. Each card has an inline pending count,
   an expand toggle, and a deep-link list of subtask items with
   priority / due / age metadata.
2. **Two new groups beyond the 7 brief sources**: `threads` (Slack
   threads the bridge has handled) and `sessions` (Claude Code
   sessions the orchestrator has resumed). Both link into the
   existing `/logs` viewer.
3. **Stat row card #4** was repurposed from "Last bridge activity"
   (low signal) to **"Pending across all platforms"** with the
   total count in NVIDIA green.
4. **NVIDIA-themed Tailwind palette** wired into `base.html`. The
   `ink.*` and `accent.*` token names are kept as aliases so every
   existing template (`source.html`, `ops.html`, `logs.html`,
   `nav.html`) auto-re-themes with zero per-file edits.
5. **Mock data layer** at `src/agent_me/dashboard/mock_pending.py`
   provides the 30 starter items across the 9 groups. Every item
   carries `mock: true` and every group footer carries
   `note: "Mock — wire to real APIs in Phase 5"` so the operator
   never confuses these placeholders for real work.

## Why mock first

Wiring 9 different real data sources at once would have stalled the
feature behind 9 OAuth / pagination / rate-limit conversations. By
landing the UI + data contract first with mock data, Phase 5 can
replace each fetcher independently:

- 7 brief-source groups (jira / gitlab / confluence / nvbugs / slack /
  outlook / github) → re-use the existing `agent-me-brief` fan-out
  fetchers (already battle-tested against the MaaS MCPs).
- `threads` group → read `state.db` `threads` + `messages` tables
  (already accessible via `StateReader.recent_threads`).
- `sessions` group → read `state.db` `claude_sessions` table
  (already accessible).

Each Phase 5 fetcher just needs to return the existing `PendingItem`
shape and `mock_pending._<source>_group()` gets swapped for the
real call. No UI changes required.

## Data contract

```python
# src/agent_me/dashboard/mock_pending.py
@dataclass
class PendingItem:
    item_id: str       # "IPP-4521" | thread_ts | session_id
    title: str         # short, human-readable
    url: str           # https://... or relative /logs?... for internal
    kind: str          # issue | mr | page | bug | msg | email | pr | thread | session
    priority: str | None     # P0 | P1 | P2 | None
    due: str | None          # "Fri" | "tomorrow" | None
    age_label: str           # "2h ago"
    mock: bool = True

@dataclass
class PendingGroup:
    group_id: str          # jira|gitlab|confluence|nvbugs|slack|outlook|github|threads|sessions
    label: str             # display name
    icon: str              # emoji
    pending_count: int     # invariant: == len(items)
    home_url: str          # "View all in <X>" link
    items: list[PendingItem]
    mock: bool = True
    note: str = "Mock — wire to real APIs in Phase 5"

def get_pending_groups() -> list[PendingGroup]:
    """Order matters — UI renders in this order."""
    return [_jira_group(), _gitlab_group(), _confluence_group(),
            _nvbugs_group(), _slack_group(), _outlook_group(),
            _github_group(), _threads_group(), _sessions_group()]

def pending_groups_dicts() -> list[dict[str, Any]]:
    """Same as get_pending_groups() but plain dicts (for Jinja/Alpine)."""
```

The route `page_index` calls `pending_groups_dicts()` and passes
both `pending_groups` (list[dict]) and `total_pending` (int) into
the template context.

## NVIDIA color tokens

Sourced from <http://api.nth.nvidia.com/static/color-swatches.html>
(NVIDIA NIM Test Hub colormap, the canonical NVIDIA internal palette).

### NV Green ladder

| Token | Hex | Use |
|---|---|---|
| `nvgreen-100` | `#cfff40` | brightest tint |
| `nvgreen-200` | `#a5de15` | bright |
| `nvgreen-300` | `#76b900` | **NVIDIA brand green (●)** — primary text/link accent |
| `nvgreen-500` | `#549a00` | hover |
| `nvgreen-700` | `#3f8500` | button bg |
| `nvgreen-900` | `#265600` | darkest tint |

### NV Gray / dark surfaces

| Token | Hex | Use |
|---|---|---|
| `nvgray-50` | `#eeeeee` | text primary |
| `nvgray-100` | `#cccccc` | text muted |
| `nvgray-300` | `#898989` | text dim |
| `nvgray-700` | `#313131` | border (matches NVIDIA dark-mode border) |
| `nvgray-800` | `#222222` | border light |
| `nvgray-900` | `#1a1a1a` | surface alt (matches NVIDIA dark-mode surface-alt) |
| `nvgray-950` | `#0a0a0a` | deeper |

### Aliases — the secret to zero per-file edits

`base.html` keeps the existing `ink.*` and `accent.*` token names
but remaps their values:

```js
ink:    { 950: '#000000', 900: '#0a0a0a', 800: '#1a1a1a',
          700: '#313131', 600: '#4b4b4b' },
accent: { 400: '#76b900', 500: '#549a00', 600: '#3f8500' },
```

Every existing `bg-ink-900`, `border-ink-700`, `text-accent-400`,
`bg-accent-600`, `hover:bg-accent-500` etc. across `source.html`,
`ops.html`, `logs.html`, `nav.html` automatically picks up the
NVIDIA hues. **No per-template edits were needed for re-theming.**
Net diff for the theme work was 2 files (`base.html`, `app.css`).

## UI — the expandable card

```
┌─────────────────────────────────────────────────┐
│ 📋  Jira    [5 pending]                  ↗  −  │  ← header (clickable to toggle)
├─────────────────────────────────────────────────┤
│ IPP-4521 Triage flaky e2e suite     [P1] due Fri 2h ago
│ IPP-4519 MaaS Glean re-index        [P2] due next Mon 1d ago
│ IPP-4502 Approve MR backlog         [P1] 3d ago
│ IPP-4498 RFC: hybrid PA + Claude    [P0] due tomorrow 4h ago
│ IPP-4480 Reset NVBugs MCP token     [P2] 5d ago
├─────────────────────────────────────────────────┤
│ Mock — wire to real APIs in Phase 5     View all in Jira → │
└─────────────────────────────────────────────────┘
```

State (per-group expand boolean) is held by a single Alpine
component `pendingPanel(groups)` defined inside the `{% block
scripts %}` of `index.html`. Default: top 3 groups (highest in the
list = highest pending in practice) start expanded, rest start
collapsed. Header has `expand all` / `collapse all` buttons.

Item rows: title is a link (`<a target=_blank>` for external https,
`<a target=_self>` for internal `/logs?...`). Priority renders as a
coloured badge (P0 rose / P1 amber / P2 gray slate); due date shows
in rose; age in muted slate.

## Files touched

| File | Net diff | Reason |
|---|---|---|
| `src/agent_me/dashboard/mock_pending.py` | **+454 (new)** | 9 group factories + dataclasses |
| `src/agent_me/dashboard/app.py` | +5 | Import + wire `pending_groups` and `total_pending` into `page_index` template context |
| `src/agent_me/dashboard/templates/base.html` | +30 | Tailwind config gains NVIDIA palette; `ink.*` and `accent.*` aliases |
| `src/agent_me/dashboard/templates/index.html` | +98 | New "Pending across platforms" section + `pendingPanel` Alpine component |
| `src/agent_me/dashboard/static/app.css` | +3 | Scrollbar colors → NVIDIA black/green; `.nv-accent-strip` utility |

Total: 4 files modified, 1 new file. ~150 lines net add.

## Fan-out execution

The implementation ran 3 subagents in parallel via the Task tool:

1. **Theme subagent** — `base.html` Tailwind config + `app.css` scrollbar.
2. **Backend subagent** — `mock_pending.py` (new) + `app.py` wiring.
3. **UI subagent** — `index.html` new section + Alpine component.

Subagents agreed on the `PendingGroup` dict shape via the
data-contract block in each prompt (not via cross-subagent reads,
which aren't supported). The three subagents touched disjoint
files except for `app.py` and `index.html`, but those are in
different subagents (B and C) and edited by different `StrReplace`
ranges, so no merge conflicts.

Wall-clock: ~3 min from dispatch to all three done.

## Smoke verification (post-deploy)

After `systemctl --user restart agent-me-dashboard`:

```bash
# Health
curl -sS http://127.0.0.1:8765/healthz
# → {"ok": true, "uptime_s": 1, "now_ms": ...}

# Overview HTML contains the 9 groups
curl -sS http://127.0.0.1:8765/ | grep -c "group_id"
# → 10 (9 groups + 1 in the Alpine template)

# Total pending matches the data invariant
uv run python -c "
from agent_me.dashboard.mock_pending import pending_groups_dicts
gs = pending_groups_dicts()
print('groups:', len(gs))
print('total:', sum(g['pending_count'] for g in gs))
for g in gs:
    assert g['pending_count'] == len(g['items'])
"
# → groups: 9
# → total: 30
```

Browser test (from an NVIDIA-VPN'd machine):

1. Open `https://agent-me.nvidia.com/`.
2. Verify the stat row shows **"Pending across all platforms: 30"**
   in NVIDIA green.
3. Verify the "Pending across platforms" section shows 9 cards
   (jira / gitlab / confluence first 3 expanded by default).
4. Click `+` on any collapsed card → it expands smoothly.
5. Click an item link → opens the upstream system (or `/logs?...`
   for threads/sessions).
6. Click `expand all` → all 9 cards open. `collapse all` → all close.

## Phase 5 — wiring real data

To turn off mock and use real fetches, replace each `_<group>_group()`
factory in `mock_pending.py` with a real fetcher:

| Group | Phase 5 fetcher |
|---|---|
| jira / gitlab / confluence / nvbugs / slack / outlook / github | Re-use `agent_me.scripts.daily_brief` per-source fan-out. Cache for ~10 min in `${STATE_DIR}/pending-cache/<source>.json` similar to `dashboard-cache/`. |
| threads | `StateReader.recent_threads(limit=10)` — already exists. Title = first user message. URL = `/logs?thread_ts=<ts>`. |
| sessions | `state.db` `claude_sessions` table — already accessible. Title = `f"session {sid[:8]} — {turn_count} turns"`. URL = `/logs?session_id=<sid>`. |

Each fetcher must:

1. Return the same `PendingItem` / `PendingGroup` shape.
2. Set `mock=False`.
3. Update `note` to something like `"Last refreshed N seconds ago"`.
4. Tolerate auth failures by returning a group with `pending_count=0`
   and an `error` field (UI needs a small follow-up to render it,
   reuse the existing `error` badge from the brief cards).

The UI in `index.html` and the route in `app.py` need **zero
changes** for Phase 5 — only `mock_pending.py` is swapped.

## Decisions locked

- **NVIDIA palette source**: `api.nth.nvidia.com/static/colormap.css`
  (NIM Test Hub canonical palette). Hex values verified against
  `--nv-green-300: #76b900` which matches the NVIDIA brand guideline
  on the public web (PMS 376C → #76B900).
- **Alias strategy**: `ink.*` and `accent.*` remap to NVIDIA palette
  instead of renaming every Tailwind class across 5 templates. Net
  diff is ~30 lines in `base.html` instead of ~300 lines spread
  across all templates.
- **Pending counts are static (mock)**: no SSE for the pending
  panel in this draft. Phase 5 fetchers can add SSE refresh if the
  static snapshot proves too stale; for now manual page reload is
  fine since the dashboard is operator-driven.
- **Internal links** (`threads`, `sessions`) point at `/logs?...`
  query-parameter form. The `/logs` page doesn't yet auto-select
  via these query params — that's a Phase 5 polish item.

## Open follow-ups

- Phase 5: replace mock fetchers (see table above).
- `/logs?thread_ts=...` and `/logs?session_id=...` auto-select the
  matching session dropdown when those params are present.
- Add a "Snooze" / "Mark done" action per row (would require a
  state file or DB column — out of scope for the read-only draft).
- Mobile: 9-card grid wraps OK but the expand toggle could be a
  full-width tap target. Phase 6 polish.
