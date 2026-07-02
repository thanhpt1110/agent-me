"""A PTY-backed host shell for the chat terminal (Ctrl+~).

``TerminalSession`` owns only the pseudo-terminal mechanics (fork a shell, read/
write, resize, kill) so it stays surface-agnostic and unit-testable; the
WebSocket glue lives in the dashboard route. This grants full shell access to the
host as the dashboard user, so the route gates it behind the operator passcode —
the same gate as ``/chat``.

Persistence: with ``tmux_session`` set, the PTY child is a **tmux client**
(``tmux new-session -A``) instead of a bare shell. Closing the WebSocket only
kills the client — the tmux session (and anything running in it, e.g. a long
``claude`` turn) survives on the host; the next connect reattaches to the same
screen. The same session is also reachable from SSH (``tmux attach``).
"""
from __future__ import annotations

import asyncio
import contextlib
import fcntl
import os
import pty
import re
import shutil
import signal
import struct
import subprocess
import termios
from collections.abc import Awaitable, Callable

_TMUX_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


def valid_tmux_name(name: str) -> bool:
    return bool(_TMUX_NAME_RE.match(name or ""))


def resolve_shell(preferred: str | None = None) -> str:
    for candidate in (preferred, os.environ.get("SHELL"), "/usr/bin/bash", "/bin/bash", "/bin/sh"):
        if candidate and os.path.exists(candidate):
            return candidate
    return "/bin/sh"


def resolve_tmux() -> str | None:
    """Path to tmux, or None when not installed (→ plain, non-persistent shell)."""
    found = shutil.which("tmux")
    if found:
        return found
    return "/usr/bin/tmux" if os.path.exists("/usr/bin/tmux") else None


def list_tmux_sessions(tmux_bin: str | None = None) -> list[dict]:
    """Live tmux sessions, most recently active first — the web sidebar lists
    these so a closed tab can always find its way back to a running session.
    Empty when tmux is missing or no server is running (not an error)."""
    tmux = tmux_bin or resolve_tmux()
    if not tmux:
        return []
    try:
        proc = subprocess.run(
            [tmux, "list-sessions", "-F",
             "#{session_name}\t#{session_created}\t#{session_attached}"
             "\t#{session_windows}\t#{session_activity}\t#{@agme_locked}"],
            capture_output=True, text=True, timeout=5,
        )
    except (OSError, subprocess.TimeoutExpired):
        return []
    if proc.returncode != 0:
        return []
    sessions: list[dict] = []
    for line in proc.stdout.splitlines():
        parts = line.split("\t")
        if len(parts) != 6 or not valid_tmux_name(parts[0]):
            continue
        name, created, attached, windows, activity, locked = parts
        with contextlib.suppress(ValueError):
            sessions.append({
                "name": name, "created": int(created or 0),
                "attached": int(attached or 0), "windows": int(windows or 0),
                "activity": int(activity or 0), "locked": locked.strip() == "1",
            })
    return sorted(sessions, key=lambda s: -s["activity"])


def _tmux_cmd(args: list[str], tmux_bin: str | None = None) -> tuple[bool, str]:
    tmux = tmux_bin or resolve_tmux()
    if not tmux:
        return False, "tmux not installed"
    try:
        proc = subprocess.run([tmux, *args], capture_output=True, text=True, timeout=5)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return False, str(exc)[:200]
    return proc.returncode == 0, (proc.stderr or proc.stdout).strip()[:200]


def create_tmux_session(name: str, *, cwd: str, shell: str,
                        tmux_bin: str | None = None) -> tuple[bool, str]:
    """Create a detached session (idempotent-ish: existing name → error from tmux)."""
    if not valid_tmux_name(name):
        return False, "invalid session name"
    return _tmux_cmd(["new-session", "-d", "-s", name, "-c", cwd, shell], tmux_bin)


def _tmux_session_id(name: str, tmux_bin: str | None = None) -> str | None:
    """Exact name → tmux session id ($N). set-option/show-options don't accept
    the `=name` exact-match syntax kill/rename do, and a bare name would
    prefix-match — the id is unambiguous."""
    ok, out = _tmux_cmd(["list-sessions", "-F", "#{session_name}\t#{session_id}"], tmux_bin)
    if not ok:
        return None
    for line in out.splitlines():
        sess, _, sid = line.partition("\t")
        if sess == name:
            return sid or None
    return None


def tmux_session_locked(name: str, tmux_bin: str | None = None) -> bool:
    """The operator's protect flag, stored IN tmux (user option @agme_locked) so
    it lives and dies with the session and survives dashboard restarts."""
    sid = _tmux_session_id(name, tmux_bin)
    if not sid:
        return False
    ok, out = _tmux_cmd(["show-options", "-t", sid, "-v", "@agme_locked"], tmux_bin)
    return ok and out.strip() == "1"


def set_tmux_session_locked(name: str, locked: bool,
                            tmux_bin: str | None = None) -> tuple[bool, str]:
    if not valid_tmux_name(name):
        return False, "invalid session name"
    sid = _tmux_session_id(name, tmux_bin)
    if not sid:
        return False, "no such session"
    if locked:
        return _tmux_cmd(["set-option", "-t", sid, "@agme_locked", "1"], tmux_bin)
    return _tmux_cmd(["set-option", "-t", sid, "-u", "@agme_locked"], tmux_bin)


def rename_tmux_session(name: str, to: str, tmux_bin: str | None = None) -> tuple[bool, str]:
    if not (valid_tmux_name(name) and valid_tmux_name(to)):
        return False, "invalid session name"
    if tmux_session_locked(name, tmux_bin):
        return False, "session is locked — unlock it first"
    return _tmux_cmd(["rename-session", "-t", f"={name}", to], tmux_bin)


def kill_tmux_session(name: str, tmux_bin: str | None = None) -> tuple[bool, str]:
    if not valid_tmux_name(name):
        return False, "invalid session name"
    if tmux_session_locked(name, tmux_bin):
        return False, "session is locked — unlock it first"
    return _tmux_cmd(["kill-session", "-t", f"={name}"], tmux_bin)


class TerminalSession:
    """One interactive shell behind a pseudo-terminal.

    With ``tmux_session`` (+ an installed tmux), the child is a tmux client
    attached to that named session (created on first use) — killing the client
    on close never kills the session, which is the whole point.
    """

    def __init__(self, *, shell: str, cwd: str, env: dict[str, str] | None = None,
                 cols: int = 80, rows: int = 24,
                 tmux_session: str | None = None, tmux_bin: str | None = None) -> None:
        self.shell = shell
        self.cwd = cwd
        self._env = env
        self.cols = cols
        self.rows = rows
        self.tmux_session = tmux_session
        self.tmux_bin = tmux_bin
        self.pid: int | None = None
        self.fd: int | None = None

    def build_argv(self) -> list[str]:
        if self.tmux_session and self.tmux_bin:
            # -A = attach when the session already exists; -c sets the start dir
            # on create; the trailing shell only applies on create too. The
            # chained set-option makes the most recently active client win the
            # window size (default "smallest" shrinks every viewer to the
            # smallest attached client — web + SSH together would squeeze both).
            return [self.tmux_bin, "new-session", "-A", "-s", self.tmux_session,
                    "-c", self.cwd, self.shell,
                    ";", "set-option", "-g", "window-size", "latest"]
        return [self.shell, "-i"]

    def start(self) -> None:
        # Build argv/env in the parent: after forkpty in a multi-threaded server
        # the child must only call async-signal-safe syscalls (chdir/execve), not
        # allocate Python objects, or it can deadlock on a lock held at fork time.
        argv = self.build_argv()
        exe = argv[0]
        env = self._child_env()
        cwd = self.cwd
        pid, fd = pty.fork()
        if pid == 0:  # child: become the shell/tmux client and never return.
            # Bare try/except (not contextlib.suppress): post-fork the child must
            # avoid allocating Python objects, so SIM105 is intentionally ignored.
            try:  # noqa: SIM105
                os.chdir(cwd)
            except OSError:
                pass
            try:  # noqa: SIM105
                os.execve(exe, argv, env)
            except OSError:
                pass
            os._exit(127)
        self.pid, self.fd = pid, fd
        flags = fcntl.fcntl(fd, fcntl.F_GETFL)
        fcntl.fcntl(fd, fcntl.F_SETFL, flags | os.O_NONBLOCK)
        self.resize(self.cols, self.rows)

    def _child_env(self) -> dict[str, str]:
        env = dict(os.environ if self._env is None else self._env)
        env.setdefault("TERM", "xterm-256color")
        env["COLUMNS"] = str(self.cols)
        env["LINES"] = str(self.rows)
        return env

    def write(self, data: str) -> None:
        if self.fd is None:
            return
        with contextlib.suppress(OSError):
            os.write(self.fd, data.encode("utf-8", "replace"))

    def resize(self, cols: int, rows: int) -> None:
        self.cols, self.rows = max(1, int(cols)), max(1, int(rows))
        if self.fd is None:
            return
        winsize = struct.pack("HHHH", self.rows, self.cols, 0, 0)
        with contextlib.suppress(OSError):
            fcntl.ioctl(self.fd, termios.TIOCSWINSZ, winsize)

    def close(self) -> None:
        if self.pid:
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.kill(self.pid, signal.SIGHUP)
            with contextlib.suppress(ProcessLookupError, PermissionError):
                os.kill(self.pid, signal.SIGTERM)
            with contextlib.suppress(ChildProcessError, OSError):
                os.waitpid(self.pid, os.WNOHANG)
            self.pid = None
        if self.fd is not None:
            with contextlib.suppress(OSError):
                os.close(self.fd)
            self.fd = None


async def stream_output(
    session: TerminalSession,
    send_text: Callable[[str], Awaitable[None]],
    *, read_size: int = 65536,
) -> None:
    """Forward PTY output to ``send_text`` until the shell exits or send fails."""
    if session.fd is None:
        return
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[bytes | None] = asyncio.Queue()
    fd = session.fd

    def _on_readable() -> None:
        try:
            data = os.read(fd, read_size)
        except (BlockingIOError, InterruptedError):
            return
        except OSError:
            data = b""
        queue.put_nowait(data or None)

    loop.add_reader(fd, _on_readable)
    try:
        while True:
            chunk = await queue.get()
            if chunk is None:
                return
            await send_text(chunk.decode("utf-8", "replace"))
    finally:
        with contextlib.suppress(ValueError, OSError):
            loop.remove_reader(fd)
