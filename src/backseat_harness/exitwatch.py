"""Observe, never prevent: leave a line before the platform kills us.

Daemon and run processes were dying with no ``finally`` -- no "guard
released", no row release, no traceback -- so the exit cause went
unrecorded. This installs a SIGINT wrapper and (on Windows) a console
ctrl handler, each writing one line *before* the platform's default
behavior proceeds: Ctrl-C receipts, console close, logoff, and shutdown
all become visible. A death with no line is itself the finding: a hard
kill no handler can see.
"""

from __future__ import annotations

import os
import signal
from collections.abc import Callable

#: Windows console ctrl event codes -> names (wincon.h).
CTRL_EVENT_NAMES = {
    0: "CTRL_C_EVENT",
    1: "CTRL_BREAK_EVENT",
    2: "CTRL_CLOSE_EVENT",
    3: "CTRL_LOGOFF_EVENT",
    4: "CTRL_SHUTDOWN_EVENT",
}


def install(note: Callable[[str], None]) -> Callable[[], None]:
    """Route exit-relevant signals through ``note``; returns a restore().

    Behavior is unchanged: SIGINT still raises KeyboardInterrupt (the
    wrapper notes first, then raises exactly like the default handler),
    and the ctypes ctrl handler returns False so Windows' default handling
    runs after the note lands. ``restore()`` puts back whatever was there.
    The ctrl callback is kept alive by this closure until restore().
    """
    previous_sigint = signal.getsignal(signal.SIGINT)

    def _sigint(signum, frame):
        note("signal: SIGINT (console Ctrl-C)")
        raise KeyboardInterrupt

    signal.signal(signal.SIGINT, _sigint)

    ctrl_handler = None
    if os.name == "nt":
        import ctypes

        handler_type = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_uint)

        def _on_ctrl(event) -> bool:
            name = CTRL_EVENT_NAMES.get(int(event), f"unknown({int(event)})")
            note(f"console ctrl: {name}")
            return False

        ctrl_handler = handler_type(_on_ctrl)
        ctypes.windll.kernel32.SetConsoleCtrlHandler(ctrl_handler, True)

    def restore() -> None:
        signal.signal(signal.SIGINT, previous_sigint)
        if ctrl_handler is not None:
            import ctypes

            ctypes.windll.kernel32.SetConsoleCtrlHandler(ctrl_handler, False)

    return restore
