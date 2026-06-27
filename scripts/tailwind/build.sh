#!/usr/bin/env bash
# Regenerate the dashboard's compiled Tailwind stylesheet (replaces the Play CDN,
# which compiled in-browser and re-scanned the DOM on every mutation — slow + laggy).
# CSS-build tooling only; no node_modules is committed (npx uses the global cache).
# Run from the repo root after changing template class names.
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"
npx --yes tailwindcss@3.4.17 \
  -c scripts/tailwind/tailwind.config.js \
  -i scripts/tailwind/input.css \
  -o src/agent_me/dashboard/static/tailwind.css \
  --minify
echo "built src/agent_me/dashboard/static/tailwind.css"
