"""PTY backend abstraction for web shell (step 8.1).

Provides WinPTYBackend (pywinpty/ConPTY) and PosixPTYBackend (ptyprocess)
behind a common interface: spawn(cmd, cwd), read, write, set_size(cols, rows), kill.
"""

from __future__ import annotations

import abc
import sys
from pathlib import Path
from typing import Protocol


class PTYBackend(Protocol):
    """Common interface for PTY backends."""

    def spawn(self, cmd: list[str], cwd: str | Path) -> None:
        """Start a new PTY process with the given command and working directory."""
        ...

    def read(self, size: int = 1024) -> bytes:
        """Read up to ``size`` bytes from the PTY."""
        ...

    def write(self, data: bytes) -> int:
        """Write ``data`` to the PTY. Returns number of bytes written."""
        ...

    def set_size(self, cols: int, rows: int) -> None:
        """Resize the PTY to ``cols`` x ``rows``."""
        ...

    def kill(self) -> None:
        """Kill the PTY process."""
        ...

    def isalive(self) -> bool:
        """Return True if the PTY process is still alive."""
        ...

    def wait(self) -> int:
        """Wait for the process to exit and return its exit code."""
        ...


class BasePTYBackend:
    """Abstract base class for PTY backends."""

    @abc.abstractmethod
    def spawn(self, cmd: list[str], cwd: str | Path) -> None:
        raise NotImplementedError

    @abc.abstractmethod
    def read(self, size: int = 1024) -> bytes:
        raise NotImplementedError

    @abc.abstractmethod
    def write(self, data: bytes) -> int:
        raise NotImplementedError

    @abc.abstractmethod
    def set_size(self, cols: int, rows: int) -> None:
        raise NotImplementedError

    @abc.abstractmethod
    def kill(self) -> None:
        raise NotImplementedError

    @abc.abstractmethod
    def isalive(self) -> bool:
        raise NotImplementedError

    @abc.abstractmethod
    def wait(self) -> int:
        raise NotImplementedError


class WinPTYBackend(BasePTYBackend):
    """Windows PTY backend using pywinpty (ConPTY)."""

    def __init__(self) -> None:
        self._process = None
        self._pty = None

    def spawn(self, cmd: list[str], cwd: str | Path) -> None:
        try:
            import winpty
        except ImportError as e:
            raise RuntimeError(
                "pywinpty is required for Windows PTY support. "
                "Install with: pip install pywinpty"
            ) from e

        # Pass argv list directly to winpty; it handles list2cmdline internally
        self._pty = winpty.PtyProcess.spawn(cmd, cwd=str(cwd))
        self._process = self._pty

    def read(self, size: int = 1024) -> bytes:
        if self._pty is None:
            return b""
        try:
            data = self._pty.read(size)
            return data.encode("utf-8", errors="replace") if isinstance(data, str) else data
        except Exception:
            return b""

    def write(self, data: bytes) -> int:
        if self._pty is None:
            return 0
        try:
            text = data.decode("utf-8", errors="replace")
            self._pty.write(text)
            # pywinpty write returns None on success; return input length
            return len(data)
        except Exception:
            return 0

    def set_size(self, cols: int, rows: int) -> None:
        if self._pty is not None:
            self._pty.setwinsize(rows, cols)

    def kill(self) -> None:
        if self._pty is not None:
            try:
                self._pty.terminate()
            except Exception:
                pass
            self._pty = None
            self._process = None

    def isalive(self) -> bool:
        if self._pty is None:
            return False
        try:
            return self._pty.isalive()
        except Exception:
            return False

    def wait(self) -> int:
        if self._pty is None:
            return -1
        try:
            return self._pty.wait()
        except Exception:
            return -1


class PosixPTYBackend(BasePTYBackend):
    """POSIX PTY backend using ptyprocess."""

    def __init__(self) -> None:
        self._process = None

    def spawn(self, cmd: list[str], cwd: str | Path) -> None:
        try:
            import ptyprocess
        except ImportError as e:
            raise RuntimeError(
                "ptyprocess is required for POSIX PTY support. "
                "Install with: pip install ptyprocess"
            ) from e

        self._process = ptyprocess.PtyProcessUnicode.spawn(cmd, cwd=str(cwd))

    def read(self, size: int = 1024) -> bytes:
        if self._process is None:
            return b""
        try:
            data = self._process.read(size)
            return data.encode("utf-8", errors="replace") if isinstance(data, str) else data
        except EOFError:
            return b""
        except Exception:
            return b""

    def write(self, data: bytes) -> int:
        if self._process is None:
            return 0
        try:
            text = data.decode("utf-8", errors="replace")
            return self._process.write(text)
        except Exception:
            return 0

    def set_size(self, cols: int, rows: int) -> None:
        if self._process is not None:
            self._process.setwinsize(rows, cols)

    def kill(self) -> None:
        if self._process is not None:
            try:
                self._process.terminate()
            except Exception:
                pass
            self._process = None

    def isalive(self) -> bool:
        if self._process is None:
            return False
        try:
            return self._process.isalive()
        except Exception:
            return False

    def wait(self) -> int:
        if self._process is None:
            return -1
        try:
            return self._process.wait()
        except Exception:
            return -1


def get_pty_backend() -> BasePTYBackend:
    """Return the appropriate PTY backend for the current platform."""
    if sys.platform == "win32":
        return WinPTYBackend()
    return PosixPTYBackend()


__all__ = [
    "PTYBackend",
    "BasePTYBackend",
    "WinPTYBackend",
    "PosixPTYBackend",
    "get_pty_backend",
]