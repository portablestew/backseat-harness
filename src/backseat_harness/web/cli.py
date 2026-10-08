"""Uvicorn runner for the web app (scaffold step 3.2).

The blocking entrypoint (:func:`run_web`) owns the process, the way `web`
always has. :func:`start_web_in_thread` is the embedded form for
``backseat-harness daemon run --web``: the caller keeps the main thread (the
daemon's pump and its Ctrl-C), this side owns uvicorn on a background thread.
Keeping both here means ``daemon.py`` imports nothing from ``web/`` -- the
daemon only starts and stops what this module returns.
"""

from __future__ import annotations

import socket
import threading
from pathlib import Path

_ALLOWED_HOSTS = {"127.0.0.1", "localhost"}

_STOP_JOIN_S = 5.0

#: Thread name of the embedded server; the one identifiable seam a probe or
#: test can look for.
WEB_THREAD_NAME = "backseat-web"


class WebHandle:
    """A running uvicorn server on a background thread; ``stop`` ends it.

    ``stop`` is the whole contract: set uvicorn's ``should_exit`` (the serve
    loop checks it every tick) and join the thread with a deadline. Idempotent
    on purpose, so both the normal shutdown and the error paths can call it.
    """

    def __init__(self, server: object, thread: threading.Thread) -> None:
        self._server = server
        self._thread = thread

    def stop(self, timeout: float = _STOP_JOIN_S) -> None:
        """Ask the server to exit and wait for the thread."""
        server = getattr(self, "_server", None)
        if server is not None:
            server.should_exit = True
            self._server = None
        self._thread.join(timeout=timeout)


def _ensure_port_free(host: str, port: int) -> None:
    """Bind-check host:port before anything depends on it.

    Used as a pre-flight so a busy port is refused **before** the daemon
    claims its single-instance guard (2-plan A/3a). uvicorn binds inside its
    serve loop on the background thread, which would otherwise race the claim.
    The check leaves a small try-this-then-that window between the probe close
    and uvicorn's bind; accepted for a localhost tool, documented in the plan.
    """
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            probe.bind((host, port))
    except OSError as exc:
        raise OSError(f"web: {host}:{port} is already in use ({exc})") from exc


def start_web_in_thread(host: str = "127.0.0.1", port: int = 8080) -> WebHandle:
    """Serve the web app on a daemon thread; return the handle that stops it.

    Same localhost-only contract as :func:`run_web` (never a non-loopback
    bind), and the same ``create_app(port=...)`` wiring -- the port feeds the
    Host/Origin allowlist middleware (``security.py``), so the real port must
    be passed through, not defaulted. ``log_config=None`` keeps uvicorn from
    installing its own logging config over the daemon's.

    Raising before start (bad host, busy port) is deliberate: ``daemon run
    --web`` treats an unusable binding as a pre-run refusal, not as a daemon
    half-started without its UI.
    """
    if host not in _ALLOWED_HOSTS:
        raise ValueError(f"refusing non-localhost bind: {host!r}")

    import uvicorn

    from backseat_harness.web.main import create_app

    app = create_app(port=port)
    server = uvicorn.Config(app, host=host, port=port, log_config=None)
    _ensure_port_free(host, port)
    uvicorn_server = uvicorn.Server(server)
    thread = threading.Thread(target=uvicorn_server.run, name=WEB_THREAD_NAME, daemon=True)
    thread.start()
    return WebHandle(uvicorn_server, thread)


def run_web(host: str = "127.0.0.1", port: int = 8080, workspace: str | Path = ".") -> None:
    """Run the web app with localhost-only binding (never 0.0.0.0)."""
    if host not in _ALLOWED_HOSTS:
        raise ValueError(f"refusing non-localhost bind: {host!r}")
    import uvicorn

    from backseat_harness.web.main import create_app

    app = create_app(port=port)
    uvicorn.run(app, host=host, port=port)
