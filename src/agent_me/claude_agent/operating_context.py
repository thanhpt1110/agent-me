"""Authoritative host/branch operating context shared by every claude surface.

Both the dashboard /chat turn and the Slack bridge inject this so the agent acts
on the real host setup: the agent-me repo, the `big-update` working branch, the
GitLab origin, no `gh`, and the auto-push commit convention. Keeping it in one
place stops the two surfaces from drifting apart.
"""
from __future__ import annotations

import os
from pathlib import Path

DEFAULT_BRANCH = "big-update"


def operating_context(cwd: str | os.PathLike[str], *, branch: str = DEFAULT_BRANCH) -> str:
    # The cwd may be the repo itself OR the workspace umbrella that contains it
    # (e.g. Slack agents run at /data/agent-me.nvidia.com) — name the repo path
    # explicitly either way so the git guidance below always has a target.
    path = Path(cwd)
    child = path / "agent-me"
    repo = child if not (path / ".git").exists() and (child / ".git").exists() else path
    where = (
        f"in the repo {repo}" if repo == path
        else f"in the workspace {path}; the agent-me repo is {repo}"
    )
    return (
        "Operating context for this machine (authoritative):\n"
        f"- You run on host agent-me.nvidia.com (CPU) {where}.\n"
        f"- Always work on the `{branch}` branch. Never switch to or commit on `main`.\n"
        "- The git remote `origin` is GitLab "
        "(gitlab-master.nvidia.com/vrdc-swqa/swqa-2e2-workflow/agent-me). "
        "The `github` remote is a public mirror only — do not push there.\n"
        "- `gh` is NOT installed; never use it. Use plain `git`.\n"
        f"- After you finish a requested change, commit it on `{branch}` with a clear "
        f"message; a post-commit hook auto-pushes `{branch}` to GitLab, so the user "
        "sees every commit on the remote.\n"
    )
