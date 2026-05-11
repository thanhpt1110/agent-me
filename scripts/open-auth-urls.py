"""agent-me — open auth URLs in browser, auto-click style.

Reads OAuth authorize URLs from stdin (one per line, or `label\\turl` tab-
separated) and emits one of these, in priority order:

    1. If `BROWSER` or `xdg-open`/`open` works (DISPLAY/macOS), opens each URL
       directly via that opener. (Local desktop case.)
    2. Otherwise, builds an HTML page with all URLs as `<a target=_blank>`
       links plus a single "Open all" button, base64-encodes it as a
       `data:text/html;base64,…` URL, and prints that URL. Cursor / VS Code
       terminal turns the `data:` URL into a hyperlink — Ctrl/Cmd-click it
       to open the page in your local default browser, then click "Open all"
       to spawn the 16 OAuth tabs. (Headless / Cursor Remote SSH case.)

Two callers:
    # Direct from a fresh reauth log:
    cd ~/agent-me && uv run python scripts/extract-reauth-urls.py | uv run python scripts/open-auth-urls.py

    # From a saved label/url file (output of extract-reauth-urls.py):
    cat /tmp/reauth-urls-final.txt | uv run python scripts/open-auth-urls.py

Why a `data:` URL and not a localhost mini-server: we don't have to keep a
listener alive, no port to forward, no cleanup. Cursor terminal hyperlinks
honor `data:text/html` and route them to the local browser via the same
`vscode.env.openExternal` channel as any other URL. The browser then runs
the page locally; the "Open all" button triggers 16 `window.open` calls,
which browsers permit because they're inside a user-gesture handler.
"""

from __future__ import annotations

import base64
import html
import os
import platform
import shutil
import subprocess
import sys
from urllib.parse import unquote


def label_for_url(url: str) -> str:
    """Best-effort short label: maas-* server name from `resource=` param."""
    import re
    m = re.search(r"resource=([^&]+)", url)
    if m:
        resource = unquote(m.group(1))
        m2 = re.search(r"/maas/([^/]+)/mcp", resource)
        if m2:
            endpoint_to_name = {
                "ippsec_metrics": "maas-ippsec",
                "jama_cache": "maas-jama",
                "colossus_mysql": "maas-mysql",
                "nsight_cuda": "maas-nsight-cuda",
                "nvks_prometheus": "maas-nvks-prometheus",
            }
            ep = m2.group(1)
            return endpoint_to_name.get(ep, f"maas-{ep}")
    # Fallback: path component
    m = re.search(r"/auth/([^/]+)/authorize", url)
    if m:
        return f"flow:{m.group(1)}"
    return url[:60] + "…"


def parse_input() -> list[tuple[str, str]]:
    """Read stdin, return list of (label, url) tuples."""
    items: list[tuple[str, str]] = []
    for line in sys.stdin:
        line = line.strip()
        if not line or not line.lower().startswith("http"):
            # Try tab-separated label\turl
            if "\t" in line:
                parts = line.split("\t", 1)
                if len(parts) == 2 and parts[1].lower().startswith("http"):
                    items.append((parts[0], parts[1]))
            continue
        items.append((label_for_url(line), line))
    return items


def pick_local_opener() -> str | None:
    """Return an opener that works on the current host, or None if headless."""
    if platform.system() == "Darwin":
        return "open"
    if (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")) and shutil.which("xdg-open"):
        return "xdg-open"
    return None


def open_local(opener: str, urls: list[str]) -> None:
    for url in urls:
        subprocess.Popen(
            [opener, url],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    print(f"  ✓ launched {opener} for {len(urls)} URL(s)")


def build_data_url_from_html(page: str) -> str:
    encoded = base64.b64encode(page.encode("utf-8")).decode("ascii")
    return f"data:text/html;base64,{encoded}"


def build_html(items: list[tuple[str, str]]) -> str:
    """Build the standalone HTML page used by both data URL and HTTP server paths."""
    import json
    rows = []
    for label, url in items:
        safe_url = html.escape(url, quote=True)
        safe_label = html.escape(label)
        rows.append(
            f'  <li><a href="{safe_url}" target="_blank" rel="noopener noreferrer">{safe_label}</a></li>'
        )
    js_urls = json.dumps([url for _, url in items])
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>agent-me — open auth tabs</title>
<style>
body{{font-family:system-ui,sans-serif;max-width:720px;margin:2em auto;padding:1em;color:#222}}
h1{{font-size:1.4em;margin-bottom:0.5em}}
button{{font-size:1.1em;padding:0.7em 1.4em;cursor:pointer;background:#0a7;color:white;border:0;border-radius:6px}}
button:hover{{background:#085}}
ul{{margin-top:1em;line-height:1.6}}
small{{color:#666}}
.note{{background:#fff8e1;padding:0.6em 1em;border-radius:6px;margin:1em 0;font-size:0.9em}}
</style></head>
<body>
<h1>agent-me — open {len(items)} OAuth tabs</h1>
<p><small>Click <b>Open all</b> once. If your browser blocks popups, allow them for this page or click each link below individually.</small></p>
<p><button id="go">Open all {len(items)} tabs</button> <span id="status"></span></p>
<div class="note">After all tabs open: sign in to NVIDIA SSO in each. They'll redirect to <code>localhost:NNNN/callback</code> — those callbacks are handled by the <code>agent-me-reauth</code> helper running on the host.</div>
<ul>
{chr(10).join(rows)}
</ul>
<script>
const urls = {js_urls};
const go = document.getElementById('go');
const status = document.getElementById('status');
go.onclick = () => {{
    let opened = 0, blocked = 0;
    urls.forEach((u, i) => {{
        setTimeout(() => {{
            const w = window.open(u, '_blank', 'noopener');
            if (w) {{ opened++; }} else {{ blocked++; }}
            status.textContent = `Opened ${{opened}}, blocked ${{blocked}} / ${{urls.length}}`;
            if (i === urls.length - 1 && blocked > 0) {{
                status.textContent += ' — allow popups, then click "Open all" again.';
            }}
        }}, i * 120);
    }});
}};
</script>
</body></html>"""


def serve_html_then_exit(html_text: str, idle_seconds: int = 300) -> str:
    """Start an ephemeral HTTP server on a random localhost port, serve the
    HTML, and return the URL. Server stays up for `idle_seconds` then exits.

    Idea: Cursor / VS Code Remote SSH auto-forwards listening localhost ports
    to the user's local machine, so a `http://localhost:NNNN/` URL printed in
    the integrated terminal becomes click-through to their default browser.
    A 23 KB data: URL trips terminal-link length limits in some emulators —
    the HTTP version is short and reliable.
    """
    import http.server
    import socket
    import socketserver
    import threading

    body_bytes = html_text.encode("utf-8")

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body_bytes)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body_bytes)

        def log_message(self, *_args, **_kwargs):  # quiet by default
            pass

    # Pick a free port. Bind to 127.0.0.1 only — Cursor's port-forward sees it.
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

    httpd = socketserver.TCPServer(("127.0.0.1", port), Handler)

    def run():
        httpd.timeout = idle_seconds
        # serve_forever runs until shutdown(); use handle_request loop with
        # a deadline so we exit cleanly even if user never connects.
        import time
        deadline = time.time() + idle_seconds
        while time.time() < deadline:
            httpd.handle_request()  # blocks until one request, or polls

    t = threading.Thread(target=run, daemon=True)
    t.start()
    return f"http://localhost:{port}/"


def main() -> int:
    items = parse_input()
    if not items:
        print("[open-auth-urls] no URLs on stdin", file=sys.stderr)
        return 2
    print(f"[open-auth-urls] got {len(items)} URL(s)")

    opener = pick_local_opener()
    if opener:
        open_local(opener, [u for _, u in items])
        return 0

    # Headless path. We have three fallbacks, in order of preference:
    #   1. Mini HTTP server on localhost:NNNN — Cursor / VS Code Remote
    #      auto-forwards listening localhost ports, so the printed URL is
    #      a click-through to the user's local browser. Short URL, fits any
    #      terminal hyperlink detector.
    #   2. data:text/html;base64,… — works without a server but the URL is
    #      ~23 KB, which some terminal hyperlink detectors truncate.
    #   3. Plain file path — user runs `cursor <path>` or scp's it.
    page = build_html(items)

    # Always also write the file as a permanent record / scp source.
    state_dir = os.path.expanduser("~/.local/state/agent-me")
    os.makedirs(state_dir, exist_ok=True)
    file_path = os.path.join(state_dir, "auth-tabs.html")
    with open(file_path, "w", encoding="utf-8") as f:
        f.write(page)

    server_url = serve_html_then_exit(page, idle_seconds=600)
    data_url = build_data_url_from_html(page)

    print()
    print("Headless host (no DISPLAY). Three ways to open all 16 tabs at once:")
    print()
    print(f"  [1] Click in Cursor terminal (recommended):  {server_url}")
    print( "      Cursor auto-forwards the port to your local browser.")
    print( "      The page has an 'Open all' button → 16 tabs spawn.")
    print( "      Server stays up for 10 minutes.")
    print()
    print(f"  [2] data: URL fallback ({len(data_url)} chars; may exceed your terminal's hyperlink length limit):")
    print(f"      {data_url}")
    print()
    print(f"  [3] HTML file at {file_path}")
    print( "      scp it to your local machine and open with `open` / `xdg-open`,")
    print(f"      or `cursor {file_path}` to view in your editor.")
    print()
    print("Server will stay up for 10 minutes (or until Ctrl+C).")
    try:
        if sys.stdin.isatty():
            print("Press Enter to release the server early.")
            input()
        else:
            # stdin is a pipe (e.g. URL list piped in) — wait for signal.
            import time
            time.sleep(600)
    except (EOFError, KeyboardInterrupt):
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
