"""Map a web chat session to the local ``claude`` CLI's on-disk session files.

A web chat session reuses the CLI's own session id (``--session-id``/``--resume``),
so deleting a chat must also delete the matching ``~/.claude/projects/<enc>/<id>.jsonl``
transcript the CLI keeps — otherwise "deleted" chats linger in ``claude --resume``.
"""
from __future__ import annotations

import os
import re
import shutil
from collections.abc import Iterable
from pathlib import Path


def claude_projects_root() -> Path:
    """Where the CLI stores per-project session transcripts.

    Honours ``CLAUDE_CONFIG_DIR`` (the CLI's own override) so tests and
    non-default installs resolve correctly; defaults to ``~/.claude``.
    """
    base = os.environ.get("CLAUDE_CONFIG_DIR") or str(Path.home() / ".claude")
    return Path(base) / "projects"


def project_dir_name(cwd: str | os.PathLike[str]) -> str:
    # The CLI encodes a cwd into a project dir by replacing every
    # non-alphanumeric character with '-' (e.g. /home/thaphan/agent-me →
    # -home-thaphan-agent-me). Hyphens map to themselves, so this is stable.
    return re.sub(r"[^A-Za-z0-9]", "-", str(cwd))


def session_files(projects_root: Path, cwd: str, session_ids: Iterable[str]) -> list[Path]:
    project = projects_root / project_dir_name(cwd)
    seen = dict.fromkeys(sid for sid in session_ids if sid)  # dedup, keep order
    return [project / f"{sid}.jsonl" for sid in seen]


def purge_local_sessions(projects_root: Path, cwd: str, session_ids: Iterable[str]) -> list[str]:
    """Best-effort delete of the CLI transcripts for ``session_ids``.

    Removes each ``<id>.jsonl`` *and* its ``<id>/`` sidecar dir (tool-results,
    sub-agent/workflow traces) so a deleted chat is fully gone from
    ``claude --resume``. Returns the file names removed. Never raises — a missing
    file or a permissions hiccup must not block the web-session delete.
    """
    project = projects_root / project_dir_name(cwd)
    removed: list[str] = []
    for sid in dict.fromkeys(sid for sid in session_ids if sid):  # dedup, keep order
        jsonl = project / f"{sid}.jsonl"
        try:
            jsonl.unlink()
            removed.append(jsonl.name)
        except FileNotFoundError:
            pass
        except OSError:
            pass
        shutil.rmtree(project / sid, ignore_errors=True)
    return removed
