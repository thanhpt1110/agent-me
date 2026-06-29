"""Locate CLI binaries (claude/codex/uv) robustly.

A systemd ``--user`` service starts with a minimal PATH that usually omits the
nvm / ``n`` node bin dirs where the npm-installed ``claude`` and ``codex`` CLIs
live, so a bare ``shutil.which("claude")`` returns nothing and the spawn dies with
``[Errno 2] No such file or directory: 'claude'``. This resolver augments the
search with the common install locations (globbing every nvm node version so it
never hardcodes one that breaks on the next upgrade).
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path


def _candidate_dirs() -> list[str]:
    home = Path.home()
    dirs = [
        home / ".local" / "bin",
        Path("/usr/local/bin"),
        Path("/opt/homebrew/bin"),
        home / ".claude" / "local",
    ]
    # node CLIs (claude, codex) are commonly installed under a node version
    # manager — glob every installed version's bin so we don't pin one.
    dirs += sorted((home / ".nvm" / "versions" / "node").glob("*/bin"), reverse=True)
    dirs.append(home / "n" / "bin")
    return [str(d) for d in dirs]


def resolve_cli_bin(name: str, *, env_var: str | None = None) -> str:
    """Absolute path to the ``name`` CLI. Order: explicit ``env_var`` (if it points
    at an existing file) → current PATH → common install dirs (nvm/n node bins,
    ~/.local/bin, homebrew). Falls back to the bare name so a PATH-correct shell
    still works."""
    if env_var and (raw := os.environ.get(env_var)):
        p = Path(raw).expanduser()
        if p.exists():
            return str(p)
    search = os.pathsep.join([os.environ.get("PATH", ""), *_candidate_dirs()])
    return shutil.which(name, path=search) or name
