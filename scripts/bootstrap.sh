#!/usr/bin/env bash
set -euo pipefail

# bootstrap.sh — initialize an agent-me deployment on a fresh host.
#
# TODO: install dependencies, fetch secrets, register cron, start always-on session.
#   - install: claude CLI, gh CLI, jq, python deps, mcp server tooling
#   - secrets: pull from 1Password / sops / vault into configs/
#   - cron: register CronCreate jobs (daily-brief, weekly review, etc.)
#   - runtime: start always-on Claude headless session on Brev / server

echo "bootstrap.sh — not yet implemented. See STATE.md for current phase."
