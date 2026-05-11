# 2026-05-11 — agent-me Avatar / Dashboard Logo

## Context

The user asked for a polished avatar for `agent-me`, a file they can use as a
workspace icon, README branding, and the primary dashboard logo.

## Decision

Use a deterministic repo-native SVG as the canonical logo source instead of a
non-reproducible generated bitmap. The visual language is black + NVIDIA green,
with a text-free autonomous robot, circuit traces, glow, and a small 4-chip
status motif that nods to `1110` without drawing visible letters or numbers.
This keeps the README/dashboard/favicons crisp and allows reproducible PNG
exports for workspaces that require raster icons.

## Assets

- Canonical SVG: `assets/agent-me-avatar.svg`
- Workspace PNG: `assets/agent-me-avatar-1024.png`
- Smaller PNG: `assets/agent-me-avatar-512.png`
- Dashboard SVG: `src/agent_me/dashboard/static/agent-me-avatar.svg`
- Dashboard touch icon PNG: `src/agent_me/dashboard/static/agent-me-avatar-512.png`
- Renderer: `scripts/render-agent-me-avatar.py`

## Integration

- README displays `assets/agent-me-avatar.svg` near the top.
- Dashboard favicon uses `/static/agent-me-avatar.svg`.
- Dashboard Apple touch icon uses `/static/agent-me-avatar-512.png`.
- Dashboard nav brand uses `/static/agent-me-avatar.svg` instead of the robot
  emoji.

## Re-render

```bash
python scripts/render-agent-me-avatar.py
```

The renderer depends on Pillow, which is already available in the repo
environment.
