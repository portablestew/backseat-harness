"""Uvicorn runner for the web app (scaffold step 3.2)."""

from __future__ import annotations

from pathlib import Path

_ALLOWED_HOSTS = {"127.0.0.1", "localhost"}


def run_web(host: str = "127.0.0.1", port: int = 8080, workspace: str | Path = ".") -> None:
    """Run the web app with localhost-only binding (never 0.0.0.0)."""
    if host not in _ALLOWED_HOSTS:
        raise ValueError(f"refusing non-localhost bind: {host!r}")
    import uvicorn

    from backseat_harness.web.main import create_app

    app = create_app(port=port)
    uvicorn.run(app, host=host, port=port)
