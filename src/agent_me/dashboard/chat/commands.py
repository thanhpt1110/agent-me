"""Discover the slash commands + skills the local ``claude`` CLI exposes.

Powers the composer's ``/`` autocomplete so the web chat feels like a real
Claude Code session on this box: typing ``/`` lists the same custom commands
(``.claude/commands/**/*.md``) and skills (``.claude/skills/*/SKILL.md``) the CLI
would offer, plus the common built-in commands. The chosen ``/name`` is sent
verbatim as the turn — the spawned CLI expands commands and can invoke skills.
"""
from __future__ import annotations

import os
import re
from dataclasses import asdict, dataclass
from pathlib import Path

_DESC_MAX = 200

# Claude Code's built-in slash commands worth surfacing in the menu so the web
# composer mirrors the TUI. Order here is the menu order (built-ins shown first).
_BUILTINS: tuple[tuple[str, str], ...] = (
    ("/context", "Visualise the current context-window usage."),
    ("/usage", "Show plan usage and rate-limit status."),
    ("/cost", "Show token usage and cost for this session."),
    ("/compact", "Summarise the conversation to free up context."),
    ("/clear", "Clear the conversation and start fresh."),
    ("/model", "Show or switch the active model."),
    ("/agents", "List/manage the available subagents."),
    ("/mcp", "Show MCP server status."),
    ("/memory", "Edit the project/user memory files."),
    ("/review", "Review the current changes for bugs and cleanups."),
    ("/init", "Generate or refresh CLAUDE.md for this repo."),
    ("/resume", "Resume a previous session."),
    ("/status", "Show account, model, and connection status."),
    ("/config", "View or change settings."),
    ("/permissions", "View or edit tool-permission rules."),
    ("/hooks", "Configure hooks for tool events."),
    ("/export", "Export the conversation to a file or clipboard."),
    ("/doctor", "Diagnose the Claude Code install/setup."),
    ("/help", "Show available commands and usage."),
)


@dataclass(frozen=True, slots=True)
class SlashCommand:
    name: str          # leading slash, e.g. "/review", "/file-bug", "/git:commit"
    description: str
    source: str        # "command" | "skill" | "builtin"

    def as_dict(self) -> dict:
        return asdict(self)


def _read_frontmatter_description(path: Path) -> str:
    try:
        text = path.read_text("utf-8", errors="replace")[:8000]
    except OSError:
        return ""
    if not text.lstrip().startswith("---"):
        return _first_meaningful_line(text)
    body = text.split("---", 2)
    block = body[1] if len(body) >= 3 else text
    lines = block.splitlines()
    for i, line in enumerate(lines):
        m = re.match(r"\s*description:\s*(.*)$", line)
        if not m:
            continue
        value = m.group(1).strip().strip("\"'")
        if value in ("", ">", "|", ">-", "|-"):  # folded/literal block scalar
            cont = []
            for nxt in lines[i + 1:]:
                if nxt.strip() and (nxt.startswith(" ") or nxt.startswith("\t")):
                    cont.append(nxt.strip())
                elif nxt.strip():
                    break
            value = " ".join(cont)
        return value[:_DESC_MAX]
    return ""


def _first_meaningful_line(text: str) -> str:
    for line in text.splitlines():
        s = line.strip().lstrip("# ").strip()
        if s:
            return s[:_DESC_MAX]
    return ""


def _commands_in(root: Path, source: str) -> list[SlashCommand]:
    if not root.is_dir():
        return []
    out: list[SlashCommand] = []
    for path in sorted(root.rglob("*.md")):
        rel = path.relative_to(root).with_suffix("")
        name = "/" + str(rel).replace(os.sep, ":")
        out.append(SlashCommand(name=name, description=_read_frontmatter_description(path), source=source))
    return out


def _skills_in(root: Path) -> list[SlashCommand]:
    if not root.is_dir():
        return []
    out: list[SlashCommand] = []
    for skill_md in sorted(root.glob("*/SKILL.md")):
        out.append(SlashCommand(
            name="/" + skill_md.parent.name,
            description=_read_frontmatter_description(skill_md),
            source="skill",
        ))
    return out


def discover_commands(repo_dir: str | os.PathLike[str], claude_home: Path | None = None) -> list[SlashCommand]:
    """Project entries first, then user, then built-ins; first name wins (dedup)."""
    repo = Path(repo_dir)
    home = claude_home or Path(os.environ.get("CLAUDE_CONFIG_DIR") or (Path.home() / ".claude"))

    found: list[SlashCommand] = []
    found += _commands_in(repo / ".claude" / "commands", "command")
    found += _skills_in(repo / ".claude" / "skills")
    found += _commands_in(home / "commands", "command")
    found += _skills_in(home / "skills")
    for plugin_skill_root in sorted((home / "plugins").rglob("skills")):
        found += _skills_in(plugin_skill_root)
    found += [SlashCommand(name=n, description=d, source="builtin") for n, d in _BUILTINS]

    deduped: dict[str, SlashCommand] = {}
    for cmd in found:
        deduped.setdefault(cmd.name, cmd)

    # Built-ins first (in their curated order — recognisable from the TUI), then
    # project/user commands, then skills, each alphabetical.
    builtin_order = {name: i for i, (name, _) in enumerate(_BUILTINS)}
    group = {"builtin": 0, "command": 1, "skill": 2}

    def sort_key(c: SlashCommand) -> tuple:
        return (group.get(c.source, 3), builtin_order.get(c.name, 0), c.name)

    return sorted(deduped.values(), key=sort_key)
