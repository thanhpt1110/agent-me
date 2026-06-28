"""A PTY-backed host shell for the chat terminal (Ctrl+~).

``TerminalSession`` owns only the pseudo-terminal mechanics (fork a shell, read/
write, resize, kill) so it stays surface-agnostic and unit-testable; the
WebSocket glue lives in the dashboard route. This grants full shell access to the
host as the dashboard user, so the route gates it behind the operator passcode —
the same gate as ``/chat``.
"""
from __future__ import annotations

import asyncio
import contextlib
import fcntl
import os
import pty
import signal
import struct
import termios
from collections.abc import Awaitable, Callable


def resolve_shell(preferred: str | None = None) -> str:
    for candidate in (preferred, os.environ.get("SHELL"), "/usr/bin/bash", "/bin/bash", "/bin/sh"):
        if candidate and os.path.exists(candidate):
            return candidate
    return "/bin/sh"


class TerminalSession:
    """One interactive shell behind a pseudo-terminal."""

    def __init__(self, *, shell: str, cwd: str, env: dict[str, str] | None = None,
                 cols: int = 80, rows: int = 24) -> None:
        self.shell = shell
        self.cwd = cwd
        self._env = env
        self.cols = cols
        self.rows = rows
        self.pid: int | None = None
        self.fd: int | None = None

    def start(self) -> None:
        # Build argv/env in the parent: after forkpty in a multi-threaded server
        # the child must only call async-signal-safe syscalls (chdir/execve), not
        # allocate Python objects, or it can deadlock on a lock held at fork time.
        shell = self.shell
        argv = [shell, "-i"]
        env = self._child_env()
        cwd = self.cwd
        pid, fd = pty.fork()
        if pid == 0:  # child: become the shell and never return.
            # Bare try/except (not contextlib.suppress): post-fork the child must
            # avoid allocating Python objects, so SIM105 is intentionally ignored.
            try:  # noqa: SIM105
                os.chdir(cwd)
            except OSError:
                pass
            try:  # noqa: SIM105
                os.execve(shell, argv, env)
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
