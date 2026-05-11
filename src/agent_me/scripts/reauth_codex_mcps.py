"""agent-me — Codex MCP re-auth helper.

Codex CLI can register NVIDIA MaaS HTTP MCP servers, but this Codex version
reports "No authorization support detected" for `codex mcp login <server>`.
agent-me therefore keeps the proven MaaS OAuth bootstrap path: refresh the
existing Claude/MaaS credential store, then inject those access tokens into
Codex MCP processes through the bearer-token env vars configured by
`scripts/setup-codex-mcps.sh`.
"""

from __future__ import annotations

import argparse
import base64
import html
import os
import platform
import re
import shutil
import socket
import socketserver
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler

URL_RE = re.compile(r"https://[^\s)\\]\"'`<>]+")
STALE_PATTERNS = (
    "Needs authentication",
    "not authenticated",
    "authentication required",
    "401",
)


def run_text(cmd: list[str], timeout_s: float = 30.0) -> str:
    res = subprocess.run(
        cmd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        timeout=timeout_s,
        check=False,
    )
    return res.stdout


def detect_stale() -> list[str]:
    out = run_text(["codex", "mcp", "list"], timeout_s=60.0)
    stale: list[str] = []
    for line in out.splitlines():
        if not any(p in line for p in STALE_PATTERNS):
            continue
        name = re.split(r"[:\s]", line.strip(), maxsplit=1)[0]
        if name.startswith("maas-") and name not in stale:
            stale.append(name)
    if stale:
        return stale

    # If Codex does not expose per-server auth state in `mcp list`, fall back
    # to all configured maas-* servers. `codex mcp login` is idempotent for
    # already-authenticated servers.
    for line in out.splitlines():
        name = re.split(r"[:\s]", line.strip(), maxsplit=1)[0]
        if name.startswith("maas-") and name not in stale:
            stale.append(name)
    return stale


def extract_auth_urls(text: str) -> list[str]:
    urls: list[str] = []
    for url in URL_RE.findall(text):
        if not (
            "authorize" in url
            or "/oauth/" in url
            or "response_type=code" in url
        ):
            continue
        if url not in urls:
            urls.append(url)
    return urls


def pick_local_opener() -> str | None:
    if platform.system() == "Darwin":
        return "open"
    if (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")) and shutil.which("xdg-open"):
        return "xdg-open"
    return None


def build_html(items: list[tuple[str, str]]) -> str:
    import json

    rows = []
    for label, url in items:
        rows.append(
            f'<li><a href="{html.escape(url, quote=True)}" target="_blank" '
            f'rel="noopener noreferrer">{html.escape(label)}</a></li>'
        )
    urls_json = json.dumps([url for _, url in items])
    return f"""<!doctype html>
<html><head><meta charset="utf-8"><title>agent-me Codex MCP auth</title>
<style>
body{{font-family:system-ui,sans-serif;max-width:760px;margin:2rem auto;padding:1rem;color:#222}}
button{{font-size:1rem;padding:.7rem 1.2rem;background:#0a7;color:white;border:0;border-radius:6px;cursor:pointer}}
li{{margin:.4rem 0;overflow-wrap:anywhere}}
code{{background:#eee;padding:.1rem .25rem;border-radius:4px}}
</style></head><body>
<h1>agent-me — Codex MCP auth</h1>
<p>Click once to open all {len(items)} OAuth tabs, then complete NVIDIA SSO in each tab.</p>
<button id="go">Open all {len(items)} tabs</button> <span id="status"></span>
<ul>{''.join(rows)}</ul>
<p><small>The <code>codex mcp login</code> processes are still running on the host and will catch localhost callbacks.</small></p>
<script>
const urls = {urls_json};
document.getElementById('go').onclick = () => {{
  let opened = 0, blocked = 0;
  urls.forEach((u, i) => setTimeout(() => {{
    const w = window.open(u, '_blank', 'noopener');
    if (w) opened++; else blocked++;
    document.getElementById('status').textContent =
      `Opened ${{opened}}, blocked ${{blocked}} / ${{urls.length}}`;
  }}, i * 120));
}};
</script></body></html>"""


def serve_html(page: str, idle_seconds: int) -> str:
    body = page.encode("utf-8")

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args, **_kwargs):
            pass

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

    httpd = socketserver.TCPServer(("127.0.0.1", port), Handler)

    def run() -> None:
        deadline = time.time() + idle_seconds
        while time.time() < deadline:
            httpd.handle_request()

    threading.Thread(target=run, daemon=True).start()
    return f"http://localhost:{port}/"


def publish_urls(items: list[tuple[str, str]], wait_s: int) -> None:
    if not items:
        return
    opener = pick_local_opener()
    if opener:
        for _, url in items:
            subprocess.Popen(
                [opener, url],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        print(f"[codex-reauth] launched {opener} for {len(items)} URL(s)")
        return

    page = build_html(items)
    state_dir = os.path.expanduser("~/.local/state/agent-me")
    os.makedirs(state_dir, exist_ok=True)
    html_path = os.path.join(state_dir, "codex-auth-tabs.html")
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(page)

    server_url = serve_html(page, idle_seconds=wait_s)
    data_url = "data:text/html;base64," + base64.b64encode(page.encode()).decode("ascii")

    print()
    print("[codex-reauth] Headless host. Open auth tabs using one of these:")
    print(f"  [1] {server_url}  (recommended in Cursor/VS Code Remote SSH)")
    print(f"  [2] {html_path}")
    print(f"  [3] data URL ({len(data_url)} chars): {data_url}")
    print()


def main() -> int:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--wait", type=int)
    parser.add_argument("--serial", action="store_true")
    args, passthrough = parser.parse_known_args()
    if args.wait is not None or args.serial:
        print(
            "[codex-reauth] ignoring Codex-native login flags; "
            "Codex MaaS OAuth is bridged through the existing MaaS token store."
        )

    print("[codex-reauth] Codex native OAuth is unsupported for MaaS HTTP MCP servers.")
    print("[codex-reauth] Refreshing the existing MaaS credential store instead.")
    print("[codex-reauth] Ensure Codex MCPs have bearer env vars: ./scripts/setup-codex-mcps.sh")

    from agent_me.scripts import reauth_mcps

    old_argv = sys.argv
    try:
        sys.argv = [old_argv[0], *passthrough]
        return reauth_mcps.main()
    finally:
        sys.argv = old_argv


if __name__ == "__main__":
    sys.exit(main())
