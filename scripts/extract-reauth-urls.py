"""agent-me — extract authorize URLs from the latest agent-me-reauth log.

Mate of `scripts/open-auth-urls.py`. Given a directory of `reauth-*.log`
files (default `~/.local/state/agent-me/`), reads the most recent one,
strips terminal ANSI escapes, stitches URLs that wrap across pty newlines,
and prints `<label>\\t<url>` lines for each unique authorize URL — one per
maas-* MCP server.

Why a separate extractor: the reauth helper writes its log to disk anyway
(when called via systemd, or when stdout is redirected for backgrounding),
and the user often wants to re-open the auth tabs without re-running
`agent-me-reauth` from scratch — tokens last hours, but the URL list is
generated fresh each run.

Usage:
    # Pipe straight into the opener:
    uv run python scripts/extract-reauth-urls.py \\
      | uv run python scripts/open-auth-urls.py

    # Or save the list:
    uv run python scripts/extract-reauth-urls.py > /tmp/auth-urls.txt
"""

from __future__ import annotations

import argparse
import glob
import os
import re
import sys
from urllib.parse import unquote

ANSI_CSI = re.compile(rb"\x1b\[[0-?]*[ -/]*[@-~]")
ANSI_OSC = re.compile(rb"\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)")
URL_CHAR = rb"A-Za-z0-9=%&_./?:~+\-"
LWRAP = re.compile(rb"(?P<a>[" + URL_CHAR + rb"])[\r\n]+(?P<b>[" + URL_CHAR + rb"])")

# Endpoint slug → MCP server name (some don't match 1:1).
ENDPOINT_TO_NAME = {
    "ippsec_metrics": "maas-ippsec",
    "jama_cache": "maas-jama",
    "colossus_mysql": "maas-mysql",
    "nsight_cuda": "maas-nsight-cuda",
    "nvks_prometheus": "maas-nvks-prometheus",
}


def find_latest_log(log_dir: str) -> str | None:
    paths = glob.glob(os.path.join(log_dir, "reauth-*.log"))
    if not paths:
        return None
    return max(paths, key=os.path.getmtime)


def clean_pty(data: bytes) -> bytes:
    data = ANSI_CSI.sub(b"", data)
    data = ANSI_OSC.sub(b"", data)
    while True:
        new = LWRAP.sub(rb"\g<a>\g<b>", data)
        if new == data:
            break
        data = new
    return data


def extract_urls(text: str) -> dict[str, str]:
    """Return label → url for each unique authorize URL found."""
    URL_CHARS = r"[A-Za-z0-9%=&._/:~+\-?]"
    candidates = re.findall(
        r"https://[A-Za-z0-9.\-]+nvidia\.com/[^\s'\"<>\)\]\x00-\x1f]*authorize\?" + URL_CHARS + "+",
        text,
    )
    required = ("response_type=", "client_id=", "code_challenge=", "redirect_uri=", "state=")
    seen: dict[str, str] = {}
    for url in candidates:
        if not all(p in url for p in required):
            continue
        # Trim glued-on English text via late lowercase→uppercase boundary.
        for m_camel in re.finditer(r"[a-z]([A-Z])", url):
            cut = m_camel.start() + 1
            if cut < 200:
                continue
            rest = url[cut:]
            if not re.search(r"[&?=]", rest):
                url = url[:cut]
                break
        # Dedupe by `resource=` rather than client_id: ECI services share
        # one client_id (`nvssa-prd-…`) across nvbugs/outlook/slack, but
        # each has its own `resource=…/maas/<server>/mcp`.
        m_res = re.search(r"resource=([^&]+)", url)
        if not m_res:
            continue
        resource = unquote(m_res.group(1))
        m_ep = re.search(r"/maas/([^/]+)/mcp", resource)
        if not m_ep:
            continue
        endpoint = m_ep.group(1)
        label = ENDPOINT_TO_NAME.get(endpoint, f"maas-{endpoint}")
        seen.setdefault(label, url)
    return seen


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--log-dir",
        default=os.path.expanduser("~/.local/state/agent-me"),
        help="directory to search for reauth-*.log",
    )
    parser.add_argument(
        "--log-file",
        help="explicit log file path (overrides --log-dir)",
    )
    args = parser.parse_args()

    log_path = args.log_file or find_latest_log(args.log_dir)
    if not log_path or not os.path.isfile(log_path):
        print(
            f"[extract-reauth-urls] no reauth log found in {args.log_dir} —"
            " run `uv run agent-me-reauth` first.",
            file=sys.stderr,
        )
        return 1

    print(f"[extract-reauth-urls] reading {log_path}", file=sys.stderr)
    with open(log_path, "rb") as f:
        data = f.read()
    text = clean_pty(data).decode("utf-8", errors="replace")
    urls = extract_urls(text)
    if not urls:
        print("[extract-reauth-urls] no authorize URLs found.", file=sys.stderr)
        return 1
    print(f"[extract-reauth-urls] found {len(urls)} URL(s)", file=sys.stderr)
    for label in sorted(urls):
        print(f"{label}\t{urls[label]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
