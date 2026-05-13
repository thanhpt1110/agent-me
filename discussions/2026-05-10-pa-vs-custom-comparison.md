# NVIDIA Personal Assistant vs custom agent-me — comparison & decision

_Written 2026-05-10. Reproducible by anyone evaluating this trade-off._

## What is NVIDIA Personal Assistant (PA)?

NVIDIA's internal AI assistant for employees. Source-of-truth pages on Glean:

- **PA CLI install & usage** — Confluence "PA CLI" (PASNT space), page 2544041986
  ```
  curl -fsSL https://nvcoworkmvp-e933cc.gitlab-master-pages.nvidia.com/install.sh | bash
  pa --version
  pa login              # SSO
  pa                    # interactive REPL
  pa -p "what meetings do I have tomorrow?"   # headless, single question
  ```
- **Architecture** — desktop + CLI client to a centralized **MCP Hub** registry at <https://mcp-hub.nvidia.com/>. Discovers & calls MCP servers across NVIDIA orgs (HR, IT, calendar, news, code, etc.). Treats Connectors / Tools / MCP servers / Skills as plug-ins. Exposed at GTC 2026.
- **Backing model** — NVIDIA-internal LLM(s); not user-selectable. Likely not Claude Opus 4.7.

PA's value-add is two-fold: (1) pre-curated NVIDIA MCP catalog, (2) polished UX team. The MCP plumbing itself is the same protocol Claude Code uses — they're peer clients.

## What we already have for the alternative

User's Claude Code installation already has these MCP servers connected (from the same MCP Hub catalog or equivalent): `jira`, `gitlab`, `glean`, `nvbugs`, `jama`, `confluence`, `gdrive`, `onedrive`, `sharepoint`, `mysql`, `ippsec`, `playwright`, `nsight-cuda`, `pagerduty`, plus the `nvinfo-cli` skill (people / rooms / desks / news / tickets). Tool coverage is effectively at parity with PA for our use case.

## Three candidate approaches

| Approach | What it is | Build effort |
|---|---|---|
| **A. Pure PA** | Install `pa` CLI, use REPL or `pa -p` directly. No agent-me, no Slack bridge. | ~5 min |
| **B. PA + Slack bridge** | Custom Slack bridge spawns `pa -p` instead of `claude -p`. Bridge handles Slack I/O; PA handles model + tools. | ~1–2 days |
| **C. Custom (current plan)** | Slack bridge spawns `claude -p` with cwd `~/agent-me/`. We bring our own MCPs (already installed). | ~2–3 days |

## Comparison table

| Criterion | A. Pure PA | B. PA + Slack | C. Custom |
|---|---|---|---|
| Setup time | 5 min | 1–2 days | 2–3 days |
| LLM model | NVIDIA-controlled, fixed | NVIDIA-controlled, fixed | **Claude Opus 4.7 (1M ctx)** |
| Tool / MCP coverage | NVIDIA MCP Hub catalog | Same | Equivalent — already installed |
| Personal / 3rd-party MCPs | Gated by NVIDIA | Gated by NVIDIA | Free |
| Repo context (CLAUDE.md, STATE.md) | None | Hacky prepend only | Native via cwd |
| Auto-memory (`~/.claude/...`) | N/A | N/A | Native |
| Out-of-box NVIDIA agents (rooms, news) | Built-in | Built-in | `nvinfo-cli` skill (manual) |
| 24/7 autonomous (cron, daily brief) | No, REPL only | Yes via bridge | Native (CronCreate, claude headless) |
| Public-shareable framework | No (NVIDIA-only) | No (PA dep) | Yes (MIT) |
| Use outside NVIDIA | No | No | Yes |
| Vendor lock-in | High (NVIDIA) | High (NVIDIA + Slack) | Low |
| Maintenance burden | NVIDIA | NVIDIA + bridge | Self |
| Risk if NVIDIA pulls tool | Total loss | PA stops; bridge useless | Unaffected |
| Approval / admin gate | None | None | None |
| Cost | NVIDIA-internal | NVIDIA-internal + Cloud host | Cloud host + Claude usage |
| Aligned with project goals¹ | Partial | Worst-of-both | Yes |

¹ Project goals: public-shareable framework (MIT template), best model (Claude Opus 4.7), 24/7 autonomous, no third-party gate, repo-aware context. See `STATE.md`.

## The single most important insight

**PA's tool coverage and our Claude Code MCP stack are interchangeable** — both are MCP clients pointed at the same NVIDIA MCP catalog. Choosing PA does not unlock new tool access; it just swaps which client orchestrates them.

So the real trade-off reduces to:

| Lever | PA wins | Custom wins |
|---|---|---|
| Setup speed | ✅ 5 min | ❌ 2–3 days |
| Polished UX (especially mobile-y desktop app) | ✅ | ❌ |
| Best LLM (per project goal) | ❌ | ✅ |
| Repo / memory / auto-context | ❌ | ✅ |
| Public-shareable framework | ❌ | ✅ |
| 24/7 autonomous workload | ❌ | ✅ |
| Independence from NVIDIA | ❌ | ✅ |

Goals stack 5–6 in favor of custom. The only meaningful concession is setup time.

## Why hybrid (B) is a trap

Approach B looks attractive on paper ("use PA for tool queries, our bridge for Slack I/O") but:

- Requires **all** the bridge work that C requires.
- Inherits **all** of PA's lock-in (NVIDIA SSO, fixed model, no public sharing).
- Adds a process boundary you don't get to debug (PA's internals are opaque).
- Forkers still can't use it — defeats the public-template purpose.

It's the worst-of-both quadrant: maximum effort, half the upside.

## Decision

**Approach C** — custom Slack bridge wrapping `claude -p` with cwd `~/agent-me/`. Continue current Phase 2 plan. As needed, fold in any missing MCP Hub servers we don't yet have via Claude Code's MCP config.

## How to defend this choice to others

> "PA is a great product if your only goal is faster onboarding to NVIDIA's tool catalog. But our agent-me has different requirements: it has to be a public-shareable framework, run on Claude Opus 4.7 specifically, carry repo + memory context across sessions, and run autonomous 24/7 jobs. PA is reactive, NVIDIA-only, and uses a fixed internal model — three constraints that conflict with the project's goals. The tools PA exposes are MCP servers; we already speak MCP and already connect to the same catalog. The only thing we genuinely give up is PA's polished UX, and we accept that trade because the framework is public and forkable."

## Caveats / things to verify later

- Confirm specific PA-exclusive features that aren't pure MCP (custom UI flows, NVIDIA-only "skills" that don't show up as MCP servers). If any are critical, revisit.
- Periodically check if MCP Hub adds servers we haven't wired into Claude Code yet — easy win for coverage parity.
- If NVIDIA ever exposes PA's backend model as a Claude Opus 4.x / GPT-4 / Llama-405B-class option, the model-quality argument weakens; revisit then.
