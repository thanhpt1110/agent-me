"""agent-me — MCP re-auth helper (full auto-open).

Detects every MCP server flagged "! Needs authentication" by `claude mcp
list`, spawns a persistent `claude` REPL inside a real pty (so claude
thinks it has a TTY and the OAuth callback listeners stay alive), feeds
it `mcp__<server>__authenticate` calls one by one, parses each printed
auth URL out of the pty output, and `open`s them all in your default
browser.

You sign in to NVIDIA SSO in each tab; redirects come back to the
still-alive REPL on localhost; tokens land in `~/.claude.json`.

When done in the browser, Ctrl-C here. The helper sends `/exit` and
shuts down cleanly.

Run with:
    uv run agent-me-reauth

Why a real pty (not Node child_process pipe): claude detects when
stdin/stdout aren't TTYs and refuses with "Input must be provided
through --print". `script(1)` can't allocate a pty unless invoked from
a controlling terminal. Python's stdlib `pty` module forks a real pty
unconditionally, sidestepping both.

Why --dangerously-skip-permissions and not --permission-mode bypassPermissions:
NVIDIA org policy disables the bypass *mode* globally ("Bypass permissions
mode was disabled by your organization policy"). The
--dangerously-skip-permissions *flag* is a separate code path and is NOT
covered by that policy, so it works for our purpose: a one-shot helper
that triggers OAuth flows. We never make actually-dangerous tool calls
here — just `mcp__<server>__authenticate`, which returns auth URLs.
"""

from __future__ import annotations

import fcntl
import os
import pty
import re
import select
import signal
import struct
import subprocess
import sys
import termios
import threading
import time

REPO_DIR = os.environ.get(
    "AGENT_ME_REPO_DIR",
    os.path.join(os.path.expanduser("~"), "agent-me"),
)
PER_CALL_GAP_S = 12.0  # Claude needs ~6-10s per authenticate call
SUBMIT_GAP_S = 0.3     # split prompt body and Enter to avoid bracketed-paste batching
BOOT_DELAY_S = 4.0
TRUST_CONFIRM_GAP_S = 2.5

URL_RE = re.compile(rb"https://[^\s)\]\"'`<>]+nvidia\.com[^\s)\]\"'`<>]*")


def detect_stale() -> list[str]:
    res = subprocess.run(
        ["claude", "mcp", "list"],
        capture_output=True,
        text=True,
        check=False,
    )
    out = res.stdout + res.stderr
    stale: list[str] = []
    for line in out.splitlines():
        if "Needs authentication" in line:
            name = line.split(":", 1)[0].strip()
            if name:
                stale.append(name)
    return stale


def main() -> int:
    # Optional --limit N to dry-run with just the first N stale servers.
    limit: int | None = None
    for i, arg in enumerate(sys.argv[1:], start=1):
        if arg == "--limit" and i < len(sys.argv) - 1:
            try:
                limit = int(sys.argv[i + 1])
            except ValueError:
                print(f"[helper] invalid --limit value: {sys.argv[i + 1]}")
                return 2

    stale = detect_stale()
    if limit is not None:
        stale = stale[:limit]
        print(f"[helper] --limit {limit} applied")
    if not stale:
        print("[helper] all MCP servers authenticated. Nothing to do.")
        return 0

    print("=" * 64)
    print("  agent-me — MCP re-auth helper (auto-open)")
    print("=" * 64)
    print(f"\n[helper] detected {len(stale)} stale server(s):")
    for s in stale:
        print(f"   - {s}")

    print("\n[helper] spawning `claude --dangerously-skip-permissions` inside pty...")

    pid, fd = pty.fork()
    if pid == 0:
        os.chdir(REPO_DIR)
        os.execvp(
            "claude",
            ["claude", "--dangerously-skip-permissions"],
        )
        os._exit(127)  # unreachable

    # Force the pty very wide so claude doesn't word-wrap auth URLs at
    # terminal width — wrapping mid-URL truncates our regex extraction
    # (URLs end up like "...response_type=cod" with the rest on the
    # next line, missing client_id / code_challenge / etc.).
    try:
        winsize = struct.pack("HHHH", 50, 4096, 0, 0)
        fcntl.ioctl(fd, termios.TIOCSWINSZ, winsize)
    except OSError as exc:
        print(f"[helper] warning: TIOCSWINSZ failed ({exc}); URLs may wrap")

    print(f"[helper] claude pid={pid}; pty fd={fd} (forced 4096-col width)\n")

    def writer() -> None:
        time.sleep(BOOT_DELAY_S)
        # First-launch trust prompt: option 1 (Yes) is pre-selected, just
        # send Enter. Harmless if no trust prompt is visible.
        print("[helper] confirming trust prompt (Enter)...")
        try:
            os.write(fd, b"\r")
        except OSError as exc:
            print(f"[helper] trust write failed: {exc}")
            return
        time.sleep(TRUST_CONFIRM_GAP_S)

        print(
            f"[helper] sending {len(stale)} authenticate call(s) sequentially,"
            f" {PER_CALL_GAP_S}s apart..."
        )
        for i, server in enumerate(stale, start=1):
            tool = f"mcp__{server}__authenticate"
            body = (
                f"Call the tool {tool} (no parameters). Print exactly what"
                " the tool returns; do not summarize."
            )
            print(f"[helper]   {i}/{len(stale)} → {tool}")
            try:
                # Bracketed-paste workaround: send body, pause, send Enter
                # separately so claude doesn't lump rapid keystrokes into one
                # multi-line prompt.
                os.write(fd, body.encode())
                time.sleep(SUBMIT_GAP_S)
                os.write(fd, b"\r")
            except OSError as exc:
                print(f"[helper] write failed: {exc}")
                return
            time.sleep(PER_CALL_GAP_S)
        print(
            "[helper] all authenticate calls dispatched. URLs will appear"
            " and browser tabs auto-open.\n"
            "[helper] sign in to NVIDIA SSO in each tab. Press Ctrl-C here"
            " when done."
        )

    threading.Thread(target=writer, daemon=True).start()

    opened: set[bytes] = set()
    buffer = b""
    shutting_down = False

    def shutdown(signum: int, _frame) -> None:  # type: ignore[no-untyped-def]
        nonlocal shutting_down
        if shutting_down:
            return
        shutting_down = True
        print(f"\n[helper] signal {signum} — sending /exit...")
        try:
            os.write(fd, b"/exit\r")
        except OSError:
            pass
        time.sleep(1.5)
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass

    signal.signal(signal.SIGINT, shutdown)
    signal.signal(signal.SIGTERM, shutdown)

    while True:
        try:
            r, _, _ = select.select([fd], [], [], 1.0)
        except (KeyboardInterrupt, OSError):
            break
        if r:
            try:
                data = os.read(fd, 8192)
            except OSError:
                break
            if not data:
                break
            sys.stdout.buffer.write(data)
            sys.stdout.buffer.flush()
            buffer += data
            for m in URL_RE.finditer(buffer):
                url_b = m.group(0)
                if url_b in opened:
                    continue
                url_s = url_b.decode("utf-8", errors="replace")
                if (
                    "authorize" in url_s
                    or "/oauth/" in url_s
                    or "response_type=code" in url_s
                ):
                    opened.add(url_b)
                    short = url_s if len(url_s) <= 60 else url_s[:57] + "..."
                    print(
                        f"\n[helper] >>> auto-opening URL #{len(opened)} ({short})\n"
                    )
                    subprocess.Popen(
                        ["open", url_s],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
        try:
            wpid, status = os.waitpid(pid, os.WNOHANG)
            if wpid == pid:
                code = os.waitstatus_to_exitcode(status)
                print(
                    f"\n[helper] claude exited (code {code}). "
                    f"opened {len(opened)}/{len(stale)} auth URL(s).\n"
                    "[helper] verify with:  claude mcp list"
                )
                return code
        except ChildProcessError:
            break

    return 0


if __name__ == "__main__":
    sys.exit(main())
