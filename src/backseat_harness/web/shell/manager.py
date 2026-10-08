"""PTY session manager (step 8.2).

Manages PTY sessions with ring-buffer replay and multi-subscriber support.
"""

from __future__ import annotations

import threading
import time
import uuid
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from backseat_harness.web.shell.pty_backend import BasePTYBackend, get_pty_backend

Subscriber = Callable[[bytes], None]


@dataclass
class PTYSession:
    """A single PTY session with its backend, buffer, and subscribers."""

    id: str
    backend: BasePTYBackend
    buffer: deque[bytes] = field(default_factory=lambda: deque(maxlen=10000))
    subscribers: set[Subscriber] = field(default_factory=set)
    lock: threading.Lock = field(default_factory=threading.Lock)
    reader_thread: threading.Thread | None = None
    dead: bool = False


class PTYSessionManager:
    """Manages PTY sessions with ring-buffer replay and multi-subscriber support."""

    def __init__(self) -> None:
        self._sessions: dict[str, PTYSession] = {}
        self._lock = threading.Lock()

    def create_session(
        self,
        cmd: list[str],
        cwd: str | Path,
        cols: int | None = None,
        rows: int | None = None,
    ) -> str:
        """Create a new PTY session and start its reader thread.

        ``cols/rows`` (plan 3g) set the initial terminal size at spawn so the
        first paint matches the page before the client's resize frame arrives.

        Returns the session ID.
        """
        session_id = uuid.uuid4().hex[:12]
        backend = get_pty_backend()
        backend.spawn(cmd, cwd, cols=cols, rows=rows)

        session = PTYSession(id=session_id, backend=backend)
        with self._lock:
            self._sessions[session_id] = session

        # Start background reader thread
        session.reader_thread = threading.Thread(
            target=self._reader_loop, args=(session_id,), daemon=True
        )
        session.reader_thread.start()

        return session_id

    def _reader_loop(self, session_id: str) -> None:
        """Background thread: read from PTY and broadcast to subscribers."""
        session = self._sessions.get(session_id)
        if session is None:
            return

        while not session.dead and session.backend.isalive():
            data = session.backend.read(1024)
            if not data:
                # No data available right now; brief sleep to avoid busy-wait,
                # but don't break - the process may still be alive and produce output later.
                time.sleep(0.01)
                continue

            with session.lock:
                session.buffer.append(data)
                subscribers = list(session.subscribers)

            for sub in subscribers:
                try:
                    sub(data)
                except Exception:
                    # Subscriber failed; detach it
                    self.detach(session_id, sub)

        # Mark session as dead
        with session.lock:
            session.dead = True

    def attach(self, session_id: str, subscriber: Subscriber) -> bool:
        """Attach a subscriber to a session.

        The subscriber immediately receives the ring-buffer replay.
        Returns True if session exists and subscriber was added.
        """
        with self._lock:
            session = self._sessions.get(session_id)

        if session is None:
            return False

        with session.lock:
            # Replay buffer to new subscriber
            for chunk in session.buffer:
                try:
                    subscriber(chunk)
                except Exception:
                    return False
            session.subscribers.add(subscriber)

        return True

    def detach(self, session_id: str, subscriber: Subscriber) -> bool:
        """Detach a subscriber from a session.

        Returns True if subscriber was found and removed.
        """
        with self._lock:
            session = self._sessions.get(session_id)

        if session is None:
            return False

        with session.lock:
            if subscriber in session.subscribers:
                session.subscribers.remove(subscriber)
                return True
        return False

    def write(self, session_id: str, data: bytes) -> int:
        """Write data to a session's PTY.

        Returns number of bytes written, or 0 if session not found.
        """
        with self._lock:
            session = self._sessions.get(session_id)

        if session is None or session.dead:
            return 0

        return session.backend.write(data)

    def resize(self, session_id: str, cols: int, rows: int) -> bool:
        """Resize the PTY for a session.

        Returns True if session exists and resize was called.
        """
        with self._lock:
            session = self._sessions.get(session_id)

        if session is None or session.dead:
            return False

        try:
            session.backend.set_size(cols, rows)
            return True
        except Exception:
            return False

    def is_alive(self, session_id: str) -> bool:
        """Check if a session's PTY is still alive."""
        with self._lock:
            session = self._sessions.get(session_id)

        if session is None or session.dead:
            return False

        return session.backend.isalive()

    def cleanup_stale(self) -> int:
        """Remove dead sessions from the manager.

        Returns the number of sessions removed.
        """
        removed = 0
        with self._lock:
            dead_ids = [
                sid for sid, s in self._sessions.items()
                if s.dead or not s.backend.isalive()
            ]
            for sid in dead_ids:
                s = self._sessions.pop(sid, None)
                if s is not None:
                    try:
                        s.backend.kill()
                    except Exception:
                        pass
                    removed += 1
        return removed

    def get_session(self, session_id: str) -> PTYSession | None:
        """Get a session by ID (for testing/inspection)."""
        with self._lock:
            return self._sessions.get(session_id)